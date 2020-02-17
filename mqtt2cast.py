#!/usr/bin/python3
"""
"""
from typing import List, Dict

import argparse
import functools
import http.server
import inspect
import json
import logging
import multiprocessing
import threading
import time
import urllib.request
import zeroconf

import paho.mqtt.client as mqtt

import pychromecast
import pychromecast.controllers.dashcast as dashcast


PARSER = argparse.ArgumentParser(description="mqtt2cast")
PARSER.add_argument("-b", "--mqtt_broker", default="192.168.1.1")
PARSER.add_argument("-p", "--mqtt_port", default=1883)
PARSER.add_argument("-d", "--dryrun", action="store_true", default=False)
PARSER.add_argument("-v", "--verbose", action="store_true", default=False)

PARSER.add_argument("-s", "--host", default="")
PARSER.add_argument("-q", "--port", default=7777)

GOOGLE_CAST_IDENTIFIER = "_googlecast._tcp.local."

ARGS = PARSER.parse_args()
if ARGS.verbose:
    logging.basicConfig(level=logging.INFO)

# sadly we have a circular dependency between these two globals:
CAST_DEVICES = None
MQTT_CLIENT = None


############################################################
# Misc Helpers
############################################################
def exception(function):
    """
    A decorator that makes sure that errors do not go unnoticed
    """
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception as err:
            logging.error("in function [%s]: %s", function.__name__, err)
            raise err
    return wrapper


def HtmlCleanup(s):
    return s.replace("<", "&lt;").replace(">", "&gt")


def ObjToDict(data):
    if hasattr(data, "_asdict"):
        return data._asdict()
    elif hasattr(data, "__slots__"):
        return data.__slots__
    elif hasattr(data, "__dict__"):
        return data.__dict__
    else:
        assert False, data


def PruneDict(d):
    out = {}
    for k, v in d.items():
        if v is None or not v and type(v) in [list, dict]:
            continue
        out[k] = v
    return out


def StrippedObject(obj):
    out = {}
    for k, v in sorted(obj.__dict__.items()):
        if v is None or not v and type(v) in [list, dict]:
            continue
        out[k] = v
    return out


class ComplexEncoder(json.JSONEncoder):
    def default(self, obj):
        return str(obj)



############################################################
# Status Page
############################################################
HTML_PROLOG = """<!DOCTYPE html>
<html>
<head>
<style>
body {
  font-family: sans-serif;
}
table {
  border-collapse: collapse;
}
</style>
</head>
<body>
<table border=1>
"""

HTML_EPILOG = """
</table>
</body>
</html>
"""


class History:
    """
    Keep some history for displaying a status page
    """

    def __init__(self):
        self._log = {}

    def log(self, host, kind, data):
        now = time.strftime("%y/%m/%d %H:%M:%S")
        logging.info("%s %s %s", host, kind, now)
        self._log[(host, kind)] = (now, data)

    def RenderStatusPage(self):
        html = []
        last = None
        for host, kind in sorted(self._log.keys()):
            timestamp, data = self._log[(host, kind)]
            if host != last:
                html.append("<tr><th colspan=3>%s</th></tr>" % host)
                last = host

            content = [str(type(data))]
            for k, v in sorted(PruneDict(ObjToDict(data)).items()):
                content.append("%s: %s" % (k, repr(v)))
            html.append("<tr><td>%s</td><td><pre>%s</pre></td><td><pre>%s</pre></td></tr>" %
                        (kind, timestamp, HtmlCleanup("\n".join(content))))
        return HTML_PROLOG + "\n".join(html) + HTML_EPILOG

############################################################
# Chrome Cast Support
# https://github.com/DeMille/url-cast-receiver
# all app ids
# https://clients3.google.com/cast/chromecast/device/baseconfig
# specific app
# https://clients3.google.com/cast/chromecast/device/app?a={}").format(app_id))
############################################################


class UrlCastController(pychromecast.controllers.BaseController):
    """ Controller to interact with Spotify namespace. """

    def __init__(self):
        super(UrlCastController, self).__init__(
            "urn:x-cast:com.url.cast", "5CB45E5A")
        self.is_launched = False

    def receive_message(self, message, data):
        """ Currently not doing anything with received messages. """
        # if data['type'] == TYPE_RESPONSE_STATUS:
        #    self.is_launched = True
        history.info("UrlCastController received: %s, %s",
                     repr(message), repr(data))
        return True

    def load_url(self, url: str, kind: str = "loc"):
        self.send_message({"type": kind, "url": url})


class CastDeviceWrapper:
    """
    Wrapps a single device
    """

    def __init__(self, host, history):
        self.history = history
        self.host = host
        # this may raise an exception
        cast = pychromecast.Chromecast(host=host)
        cast.wait()
        self.name = cast.device.friendly_name
        logging.info("found device: %s at %s", self.name, self.host)
        cast.dashcast = UrlCastController()
        cast.register_handler(cast.dashcast)
        cast.register_status_listener(self)
        cast.media_controller.register_status_listener(self)
        cast.register_launch_error_listener(self)
        cast.register_connection_listener(self)
        self.cast = cast
        mc = cast.media_controller
        self.history.log(self.host, "device_status", cast.device)
        self.history.log(self.host, "cast_status", cast.status)
        self.history.log(self.host, "media_status", mc.status)

    def EmitMessage(self, event, data):
        global MQTT_CLIENT
        MQTT_CLIENT.EmitMessage("/mqtt2cast/%s/%s" %
                                (self.name, event), json.dumps(ObjToDict(data), cls=ComplexEncoder))
        self.history.log(self.host, event, data)

    # callback API for chrome cast
    @exception
    def new_cast_status(self, status):
        self.EmitMessage("cast_status", status)

    # callback API for chrome cast
    @exception
    def new_launch_error(self, launch_failure):
        self.EmitMessage("launch_error", launch_error)

    # callback API for chrome cast
    @exception
    def new_media_status(self, status):
        self.EmitMessage("media_status", status)

    # callback API for chrome cast
    @exception
    def new_connection_status(self, status):
        self.EmitMessage("connection_status", status)

    def PlaySong(song_url: str, mime_type="audio/mpeg3"):
        logging.info("PLAY %s %s", song_url, mime_type)
        mc = self.cast.media_controller
        # mc.stop()
        self.history.log(self.host, "play_url", song_url)
        mc.play_media(song_url, mime_type)
        mc.block_until_active()
        self.history.log(self.host, "cast_status", repr(
            self.cast.status))
        self.history.log(self.host, "media_status",
                         repr(StrippedObject(mc.status)))

    def LoadUrlOnCast(url: str):
        self.cast.quit_app()
        dc = self.cast.dashcast
        self.history.log(self.host, "load_url", url)
        dc.launch()
        logging.info("WAIT LoadUrlOnCast")
        for i in range(10):
            if dc.is_active:
                break
            time.sleep(0.1)
            dc.load_url(url)


class CastDeviceManager:
    """
    Manages all the cast devices in the network
    """

    def __init__(self, history):
        self.history = history
        self.host_map = {}
        self.name_map = {}
        self.UpdateCastDevices()

    def _RegisterCastDevice(self, host):
        try:
            cast = CastDeviceWrapper(host, self.history)
            self.host_map[cast.host] = cast
            self.name_map[cast.name] = cast
        except Exception as err:
            self.history.log(host, "registration_error", str(err))

    # part of the zeroconf listener api
    def remove_service(self, zc, type, name):
        pass

    # part of the zeroconf listener api
    def add_service(self, zc, type, name):
        info = zc.get_service_info(type, name)
        ips = zc.cache.entries_with_name(info.server.lower())
        for dnsaddress in ips:
            host = repr(dnsaddress)
            self._RegisterCastDevice(host)

    def UpdateCastDevices(self):
        # note there is also pychromecast.get_chromecasts()
        zc = zeroconf.Zeroconf()
        browser = zeroconf.ServiceBrowser(
            zc, GOOGLE_CAST_IDENTIFIER, self)

    def GetCasts(self, host: str):
        if not host:
            return list(self.host_map.keys())
        if host in self.host_map:
            return [self.host_map[host]]
        if host in self.name_map:
            return [self.name_map[host]]
        return []

    def PlaySong(self, host: str, song_url: str):
        for cast in self.GetCasts(host):
            cast.PlaySong(song_url)

    def LoadUrlOnCast(self, host, url: str):
        for cast in self.GetCasts(host):
            cast.LoadUrlOnCast(url)

    def __str__(self):
        out = []
        for dev, cast in self.device_map.items():
            out.append("%s: %s" % (dev, cast.name))
        return "\n".join(out)


class MqttClient:

    def __init__(self, name, host, port, dispatcher: Dict=[]):
        self.name = name
        self.dispatcher = dispatcher
        self.client = mqtt.Client(name)
        self.client.will_set("/" + name + "/sys/status", "0", retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_log = self.on_log
        self.client.connect(host, port, keepalive=60)
        # note, this does not block
        self.client.loop_start()

    # paho API - problems will be silently ignored without this
    def on_log(client, userdata, level, buff):
        print ("!!!!!!!!!!!!!!!!!")
        log.error("paho problem %s %s %s", userdata, level, buff)

    def EmitMessage(self, topic, message, retain=True):
        logging.info("MQTT %s %s", topic, message)
        self.client.publish(topic, message, retain)

    def EmitStatusMessage(self):
        self.EmitMessage("/" + self.name + "/sys/status", "1", retain=True)

    # in its infinite wisdom, paho silently drops errors in callbacks
    @exception
    def on_connect(self, client, userdata, rc, dummy):
        logging.info("Connected with result code %s %s %s (if you see a lot of these you may have duplicate client names)",
                     rc, userdata, dummy)
        self.EmitStatusMessage()
        # Subscribing in on_connect() means that if we lose the connection and
        # reconnect then subscriptions will be renewed.
        for sub, _ in self.dispatcher:
            logging.info("subscribing to mqtt topic [%s]", sub)
            self.client.subscribe(sub)

    # in its infinite wisdom, paho silently drops errors in callbacks
    @exception
    def on_message(self, client, userdata, msg):
        """allback for when a PUBLISH message is received from the server"""
        logging.info("TRIGGER: %s %s %s", msg.topic,
                     msg.payload, userdata)
        for sub, action in self.dispatcher:
            if mqtt.topic_matches_sub(sub, msg.topic):
                try:
                    action(msg.topic.split("/"), msg.payload)
                except Exception as err:
                    logging.error("failure: %s", str(err))
                break
        else:
            logging.info("message did no match")


############################################################
# Cast devices cannot play back playlists
# The code below helps extracting songs from playlists
############################################################
def GetSongs(url):
    global URL_MAP
    # if url.startswith("@"):
    #    return [URL_MAP[url[1:]]]
    songs = [url]
    if url.endswith("pls"):
        data = urllib.request.urlopen(url).read()
        songs = GetPlsSongs(data)
    elif url.endswith("m3u"):
        data = urllib.request.urlopen(url).read()
        songs = GetM3uSongs(data)
    return songs


@exception
def PlayMediaWrapper(topic: List[str], payload: bytes):
    global CAST_DEVICES
    host = topic[3]
    url = str(payload, 'utf-8')
    logging.info("PlayMediaWrapper %s %s", host, url)
    songs = GetSongs(url)
    logging.info("Songs [%s]: %s", url, songs)
    if not songs:
        return
    CAST_DEVICES.PlaySong(host, songs[0])


@exception
def StopMediaWrapper(topic: List[str], payload: bytes):
    global CAST_DEVICES
    host = topic[3]
    CAST_DEVICES.PlaySong(host, "")


def PlayAlarmWrapper(topic: List[str], payload: bytes):
    if len(topic) == 1:
        topic.append("ALL")
    if not payload:
        payload = bytes(URL_MAP["alarm"], "utf-8")
    PlayRadioWrapper(topic, payload)


def LoadUrlWrapper(topic: List[str], payload: bytes):
    host = topic[1]
    url = str(payload, 'utf-8')
    casts = GetCasts(host)
    for cast in casts:
        LoadUrlOnCast(cast, url)


DISPATCH = [
    ("/mqtt2cast/action/scan/#", None),
    ("/mqtt2cast/action/play_media/#", PlayMediaWrapper),
    ("/mqtt2cast/action/alarm/#", PlayAlarmWrapper),
    ("/mqtt2cast/action/stop_radio/#", StopMediaWrapper),
    ("/mqtt2cast/action/load_url/#", LoadUrlWrapper),
]


HISTORY = History()


class SimpleHTTPRequestHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        global HISTORY
        self.send_response(200)
        self.end_headers()
        self.wfile.write(bytes(HISTORY.RenderStatusPage(), "utf-8"))


logging.info("start device manager")
CAST_DEVICES = CastDeviceManager(HISTORY)

logging.info("starting mqtt handler")
MQTT_CLIENT = MqttClient("mqtt2cast", ARGS.mqtt_broker,
                         ARGS.mqtt_port, DISPATCH)


logging.info("starting web interfaces")
WEB_SERVER = http.server.HTTPServer(
    (ARGS.host, ARGS.port), SimpleHTTPRequestHandler)
WEB_SERVER.serve_forever()
