## MQTT <-> Google Cast Bridge



### MQTT messages

#### Subscribed

* `/mqtt2cast/action/play_media/<friendly name>`   `<url>`
  Play/Show the given media file
* `/mqtt2cast/action/load_url/<friendly name>`   `<url>`
  Display a URL (does not work for all URLs)
  
### Published

* `/mqtt2cast/<friendly-name>/<event_kind>` `<json>` 


### Webserver

A simple webserver shows the most recents events for each device

### Deps

* pychromecast
* paho.mqtt

### Author

robert@muth.org


