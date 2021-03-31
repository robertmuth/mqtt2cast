## MQTT <-> Google Cast Bridge


Mote: this is very much work in progress.


Typical invocation

```
./mqtt2cast.py --verbose --use_zeroconf --mqtt_broker MQTT-BROKER 

```

### MQTT messages

#### Messages that the bridges is listening for

* `mqtt2cast/action/CAST-DEVICE/play_media`   `<url> [<mime_type>]`
  Play/Show the given media file (can be a playlist
* `mqtt2cast/action/CAST-DEVICE/load_url`   `<url>`
  Display a URL (does not work for all URLs)
* `mqtt2cast/action/CAST-DEVICE/queue_next`
  Skip current song - extremely flaky, needs more work
* `mqtt2cast/action/CAST-DEVICE/set_volume`  `<value between 0 and 1>`
  
CAST-DEVICE is on off:
* ip-address
* friendly name
* empty string which means all devices will receive the command

Testing
```
mosquitto_pub -h BORKER -t "mqtt2cast/action/CAST-DEVICE/play_media" -m "http://somafm.com/lush130.pls"

```

  
#### Message that the bridge is emitting

* `mqtt2cast/event/FRIENDLY-NAME/<event_kind>` `<json>` 

Testing
```
mosquitto_sub -v -h MQTT-BROKER -t "mqtt2cast/#"
```

### Webserver

A simple webserver shows the most recents events for each device. Default port is localhost:7777

### Deps

* pychromecast
* paho.mqtt



### Author

robert@muth.org


