## MQTT <-> Google Cast Bridge


Typical invocation

```
./mqtt2cast.py -v --use_zeroconf

```

### MQTT messages

#### Subscribed

* `/mqtt2cast/action/play_media/<friendly name>`   `<url>`
  Play/Show the given media file
* `/mqtt2cast/action/load_url/<friendly name>`   `<url>`
  Display a URL (does not work for all URLs)
  
  
Testing
```
mosquitto_pub -h 192.168.1.1 -t "/mqtt2cast/action/play_media/Kitchen Speaker" -m "http://somafm.com/lush130.pls"

```

  
#### Published

* `/mqtt2cast/<friendly-name>/<event_kind>` `<json>` 

Testing
```
mosquitto_sub -v -h 192.168.1.1 -t "/mqtt2cast/#"
```

### Webserver

A simple webserver shows the most recents events for each device. Default port is localhost:7777

### Deps

* pychromecast
* paho.mqtt



### Author

robert@muth.org


