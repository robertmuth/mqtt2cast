#!/usr/bin/python3
"""
Simple MQTT to Google Cast Bridge

TODO: cyclic dependencied
"""
from typing import List, Dict, Optional, Any, Tuple

import argparse
import functools
import http.server
import inspect
import json
import logging
import concurrent.futures
import threading
import time
import platform
import urllib.request
import zeroconf
import ipaddress
import urllib
import paho.mqtt.client as mqtt

import pychromecast
import pychromecast.controllers.dashcast as dashcast
# needs casttube
# import pychromecast.controllers.youtube as youtube


PARSER = argparse.ArgumentParser(description="mqtt2cast")
PARSER.add_argument("--mqtt_broker", default="192.168.1.1")
PARSER.add_argument("--mqtt_port", default=1883)
PARSER.add_argument("--dryrun", action="store_true", default=False)
PARSER.add_argument("--verbose", action="store_true", default=False)
PARSER.add_argument("--debug", action="store_true", default=False)
PARSER.add_argument("--use_zeroconf", action="store_true", default=False)
PARSER.add_argument("--scan_subnets", action='append',
                    help="scan this subnet (e.g. '192.168.1.0/24') potentially beside using zeroconf")
PARSER.add_argument("--host", default="",
                    help="hostname to use for debug webserver")
PARSER.add_argument("--port", default=7777,
                    help="port to use for debug webserver")

GOOGLE_CAST_IDENTIFIER = "_googlecast._tcp.local."

ARGS = PARSER.parse_args()

if ARGS.verbose:
    logging.basicConfig(level=logging.INFO)

if ARGS.debug:
    logging.basicConfig(level=logging.DEBUG)

if not ARGS.use_zeroconf and not ARGS.scan_subnets:
    print("you must specify either --use_zeroconf or at least one --scan_subnets=...")
    quit(1)

# sadly we have a circular dependency between these two globals:
CAST_DEVICES: Optional["CastDeviceManager"] = None
MQTT_CLIENT: Optional["MqttClient"] = None


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
        return {"payload": str(data)}


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
"""

HTML_EPILOG = """
</body>
</html>
"""


HISTORY_LOG: Dict[Tuple[Any, Any], Any] = {}


def LogHistory(host, kind, data):
    global HISTORY_LOG
    now = time.strftime("%y/%m/%d %H:%M:%S")
    logging.info(f"{host} {kind} {now}")
    HISTORY_LOG[(host, kind)] = (now, data)


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

    def __init__(self, host):
        self.host = host
        # this may raise an exception
        cast = pychromecast.Chromecast(host=host)
        cast.wait()
        self.name = cast.device.friendly_name
        logging.info("found device: [%s] at %s", self.name, self.host)
        # cast.dashcast = UrlCastController()
        cast.dashcast = dashcast.DashCastController()
        cast.register_handler(cast.dashcast)
        # youtube controller is broken
        # cast.yt = youtube.YouTubeController()
        # cast.register_handler(cast.yt)
        cast.register_status_listener(self)
        cast.media_controller.register_status_listener(self)
        cast.register_launch_error_listener(self)
        cast.register_connection_listener(self)
        self.cast = cast
        mc = cast.media_controller
        LogHistory(self.host, "device_status", cast.device)
        LogHistory(self.host, "cast_status", cast.status)
        LogHistory(self.host, "media_status", mc.status)

    def EmitMessage(self, event, data):
        global MQTT_CLIENT
        MQTT_CLIENT.EmitMessage(
            f"chromecast/{self.name}/{event}",
            json.dumps(ObjToDict(data), cls=ComplexEncoder))
        LogHistory(self.host, event, data)

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

    def PlayMedia(self, song_url: str, mime_type="audio/mpeg3"):
        logging.info("PlayMedia %s %s", song_url, mime_type)
        mc = self.cast.media_controller
        # print ("BEFORE", mc.is_playing, mc.is_paused, mc.is_idle, mc.title)
        LogHistory(self.host, "play_url", song_url)
        # mc.stop()
        # mc.block_until_active()
        # mc.play_media(song_url, mime_type, stream_type="LIVE")
        mc.play_media(song_url, mime_type)
        # print ("AFTER", mc.is_playing, mc.is_paused, mc.is_idle, mc.title)
        # self.history.log(self.host, "cast_status", self.cast.status)
        # self.history.log(self.host, "media_status", mc.status)

    def PlayYoutube(self, video_id: str):
        logging.info("PlayYoutube %s", song_id)
        yt = self.cast.yt
        LogHistory(self.host, "play_video", video_id)
        yt.play_video(video_id)

    def LoadUrl(self, url: str):
        self.cast.quit_app()
        dc = self.cast.dashcast
        LogHistory(self.host, "load_url", url)
        dc.load_url(url)
        return
        dc.launch()
        logging.info("WAIT LoadUrl")
        for i in range(10):
            if dc.is_active:
                break
            time.sleep(0.1)
            dc.load_url(url)


class CastDeviceManager:
    """
    Manages all the cast devices in the network
    """

    def __init__(self):
        self.host_map: Dict[str, CastDeviceWrapper] = {}
        self.name_map: Dict[str, CastDeviceWrapper] = {}
        self.UpdateCastDevices()

    def _RegisterCastDevice(self, host):
        try:
            cast = CastDeviceWrapper(host)
            logging.info(f"adding host: [{cast.host}]")
            self.host_map[cast.host] = cast
            logging.info(f"adding name: [{cast.name}]")
            self.name_map[cast.name] = cast
        except Exception as err:
            if not isinstance(err, pychromecast.error.ChromecastConnectionError):
                logging.error(
                    f"registration failed for {host}: {type(err)} {err}")
            # self.history.log(host, "registration_error", str(err))

    # part of the zeroconf listener api
    @exception
    def remove_service(self, zc, type, name):
        pass

    # part of the zeroconf listener api
    @exception
    def add_service(self, zc, type, name):
        info = zc.get_service_info(type, name)
        ips = zc.cache.entries_with_name(info.server.lower())
        assert ips
        for dnsaddress in ips:
            host = repr(dnsaddress)
            self._RegisterCastDevice(host)

    def UpdateCastDevices(self):
        if ARGS.use_zeroconf:
            # note there is also pychromecast.get_chromecasts()
            zc = zeroconf.Zeroconf()
            browser = zeroconf.ServiceBrowser(
                zc, GOOGLE_CAST_IDENTIFIER, self)
        if ARGS.scan_subnets:
            hosts = []
            for sn in ARGS.scan_subnets:
                hosts += [h.compressed for h in ipaddress.ip_network(sn)]
            with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
                executor.map(self._RegisterCastDevice, hosts)

    def GetCasts(self, host: str):
        if not host:
            return list(self.host_map.keys())
        if host in self.host_map:
            return [self.host_map[host]]
        if host in self.name_map:
            return [self.name_map[host]]
        logging.warning("host not found: [%s] %s", host, self.name_map.keys())
        return []

    def PlayMedia(self, host: str, song_url: str):
        for cast in self.GetCasts(host):
            cast.PlayMedia(song_url)

    def PlayYoutube(self, host: str, video_id: str):
        for cast in self.GetCasts(host):
            cast.PlayYoutube(video_id)

    def LoadUrl(self, host, url: str):
        for cast in self.GetCasts(host):
            cast.LoadUrl(url)

    def __str__(self):
        out = []
        for dev, cast in self.device_map.items():
            out.append("%s: %s" % (dev, cast.name))
        return "\n".join(out)


class MqttClient:

    def __init__(self, name, host, port, dispatcher: List = [Tuple[str, Any]]):
        self.name = name
        self.dispatcher = dispatcher
        self.client = mqtt.Client(name)
        self.client.will_set(
            f"{name}/{platform.node}/sys/status", "0", retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_log = self.on_log
        self.client.connect(host, port, keepalive=60)
        # note, this does not block
        self.client.loop_start()

    # paho API - problems will be silently ignored without this
    def on_log(client, userdata, level, buff):
        print("!!!!!!!!!!!!!!!!!")
        log.error("paho problem %s %s %s", userdata, level, buff)

    def EmitMessage(self, topic, message, retain=True):
        logging.info("MQTT %s %s", topic, message)
        self.client.publish(topic, message, retain)

    def EmitStatusMessage(self):
        self.EmitMessage(
            f"{self.name}/{platform.node}/sys/status", "1", retain=True)

    # in its infinite wisdom, paho silently drops errors in callbacks
    @exception
    def on_connect(self, client, userdata, rc, dummy):
        logging.info(
            "Connected with result code %s %s %s (if you see a lot of these you may have duplicate client names)",
            rc,
            userdata,
            dummy)
        self.EmitStatusMessage()
        # Subscribing in on_connect() means that if we lose the connection and
        # reconnect then subscriptions will be renewed.
        for sub, _ in self.dispatcher:
            logging.info(f"subscribing to mqtt topic [{sub}]")
            self.client.subscribe(sub)

    # in its infinite wisdom, paho silently drops errors in callbacks
    @exception
    def on_message(self, client, userdata, msg):
        """allback for when a PUBLISH message is received from the server"""
        logging.info(f"received: {msg.topic} {msg.payload}")
        try:
            for sub, action in self.dispatcher:
                if mqtt.topic_matches_sub(sub, msg.topic):
                    print("match: ", sub)
                    action(msg.topic.split("/"), msg.payload)
                    break
            else:
                logging.warning("message did no match")
        except Exception as err:
            logging.error("failure: %s", str(err))


############################################################
# Cast devices cannot play back playlists
# The code below helps extracting songs from playlists
############################################################
def GetPlsSongs(data):
    out = []
    for line in str(data, "utf-8").split("\n"):
        logging.info(f"line: [{line}]")
        if line.startswith("File"):
            out.append(line.split("=", 1)[1].strip())
    return out


def GetM3uSongs(data):
    out = []
    for line in str(data, "utf-8").split("\n"):
        logging.info(f"line: [{line}]")
        if line.startswith("#"):
            continue
        out.append(line.strip())
    return out


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


def PlayMediaWrapper(topic: List[str], payload: bytes):
    global CAST_DEVICES
    host = topic[3]
    url = payload.decode('utf-8')
    logging.info("PlayMediaWrapper %s %s", host, url)
    songs = GetSongs(url)
    logging.info("Songs [%s]: %s", url, songs)
    if not songs:
        return
    CAST_DEVICES.PlayMedia(host, songs[0])


def PlayYoutubeWrapper(topic: List[str], payload: bytes):
    global CAST_DEVICES
    host = topic[4]
    video_id = payload.decode('utf-8')
    logging.info("PlayYoutubeWrapper %s %s", host, video_id)
    CAST_DEVICES.PlayYoutube(host, video_id)


def StopMediaWrapper(topic: List[str], payload: bytes):
    global CAST_DEVICES
    host = topic[3]
    CAST_DEVICES.PlayMedia(host, "")


# def PlayAlarmWrapper(topic: List[str], payload: bytes):
#     if len(topic) == 1:
#         topic.append("ALL")
#     if not payload:
#         payload = bytes(URL_MAP["alarm"], "utf-8")
#     PlayRadioWrapper(topic, payload)


def LoadUrlWrapper(topic: List[str], payload: bytes):
    global CAST_DEVICES
    host = topic[3]
    url = str(payload, 'utf-8')
    CAST_DEVICES.LoadUrl(host, url)


def RescanDevices(topic: List[str], payload: bytes):
    global CAST_DEVICES
    CAST_DEVICES.UpdateCastDevices()


ACTION_MAP = {"rescan":  RescanDevices,
              "play_media": PlayMediaWrapper,
              # "play_youtube": PlayYoutubeWrapper,
              "stop_media": StopMediaWrapper,
              "load_url": LoadUrlWrapper
              }

DISPATCH = [(f"chromecast/action/{key}/#", val)
            for key, val in ACTION_MAP.items()]
# ("/mqtt2cast/action/alarm/#", PlayAlarmWrapper),


############################################################

def RenderStatusPage(history_log, cast_devices):
    global HTML_PROLOG, HTML_EPILOG
    html = ["<table border=1>"]
    last = None
    for host, kind in sorted(history_log.keys()):
        cast = cast_devices.host_map[host]
        timestamp, data = history_log[(host, kind)]
        if host != last:
            html.append(f"<tr><th colspan=3>{host} {cast.name}</th></tr>")
            last = host

        content = [str(type(data))]
        for k, v in sorted(PruneDict(ObjToDict(data)).items()):
            content.append("%s: %s" % (k, repr(v)))
        html.append(
            "<tr><td>%s</td><td><pre>%s</pre></td><td><pre>%s</pre></td></tr>" %
            (kind, timestamp, HtmlCleanup(
                "\n".join(content))))
    html += ["</table>"]
    html += ["<hr>",
             "<pre>",
             "Note the devices and actions listed below also reflect the available mqtt commands, e.g.:",
             "topic is chromecast/action/ACTION/DEVICE payload contain the argument",
             "</pre>",
             "<form action=/action method=post>"]

    html += ["<select name=device>"]
    html += [f"<option value='{name}'>{name}</option>" for name in cast_devices.name_map.keys()]
    html += ["</select>"]

    html += ["<select name=action>"]
    html += [f"<option value={action}>{action}</option>" for action in ACTION_MAP.keys()]
    html += ["</select>"]

    html += ["<input type=text name=arg>",
             "<input type=submit value=Send>",
             "</form>"]

    return HTML_PROLOG + "\n".join(html) + HTML_EPILOG


class SimpleHTTPRequestHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        print("GET", self.path)
        global HISTORY_LOG, CAST_DEVICES
        self.send_response(200)
        self.end_headers()
        self.wfile.write(bytes(RenderStatusPage(
            HISTORY_LOG, CAST_DEVICES), "utf-8"))

    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        print("DATA", post_data.decode('utf-8'))
        fields = urllib.parse.parse_qs(post_data.decode('utf-8'))
        print(fields)
        action = fields.get("action", ["scan"])[0]
        device = fields.get("device", [""])[0]
        arg = fields.get("arg", [""])[0]
        logging.info(f"web action [{action}] [{device}] [{arg}]")
        wrapper = ACTION_MAP.get(action, RescanDevices)
        wrapper(["", "", "", "", device], arg.encode("utf-8"))
        self.send_response(301)
        self.send_header('Location', '/')
        self.end_headers()


logging.info("starting mqtt handler")
MQTT_CLIENT = MqttClient("mqtt2cast", ARGS.mqtt_broker,
                         ARGS.mqtt_port, DISPATCH)

logging.info("start device manager")
CAST_DEVICES = CastDeviceManager()


logging.info("starting web interfaces on port %d", ARGS.port)
WEB_SERVER = http.server.HTTPServer(
    (ARGS.host, ARGS.port), SimpleHTTPRequestHandler)
WEB_SERVER.serve_forever()
