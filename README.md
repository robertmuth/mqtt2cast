## MQTT <-> Google Cast Bridge



### MQTT messages

#### Subscribed

* `/mqtt2cast/action/play_media/<friendly name>`   `<url>`
  Play/Show the given media file
* `/mqtt2cast/action/load_url/<friendly name>`   `<url>`
  Display a URL (does not work for all URLs)
  
  
Examples
```
mosquitto_pub -h 192.168.1.1 -t "/mqtt2cast/action/play_media/Kitchen Speaker" -m "http://somafm.com/lush130.pls"

```

  
### Published

* `/mqtt2cast/<friendly-name>/<event_kind>` `<json>` 


### Webserver

A simple webserver shows the most recents events for each device

### Deps

* pychromecast
* paho.mqtt



### Author

robert@muth.org


