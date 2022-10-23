# Meross to Homie bridge

A simple bridge between Meross devices and home automation software compatible with Homie, such as openHAB.

<div style="text-align: center">
<img src="https://homieiot.github.io/img/works-with-homie.svg" alt="Works with Homie" width="200">
</div>

## Vendor cloud NOT supported

And I have no intention to support it, ever. The plugs work perfectly fine in the local network and there's no need to
inform the vendor on what you're doing with them.

See [MQTT server setup](#mqtt-server-setup) and [Pairing to the custom broker](#pairing-to-the-custom-broker) for more
details.

## Supported hardware

Since I only own Meross MSS310 smart plugs I can only guarantee it works with those. It should work with other Meross
devices as well, but I can't guarantee it.

I use the library [MerossIoT](https://github.com/albertogeniola/MerossIot) library for the communication, so anything it
supports can be added without too much effort (pull-requests welcome).

The following capabilities are currently supported:

- Bind (auto-acknowledge device MQTT configuration)
- Toggle/ToggleX (switch plug on/off)
- Consumption/ConsumptionX (historical and daily power consumption)
- Electricity: (current voltage, current and power)
- Do not disturb (device LED setting)

## Installation

### Docker

Use the provided Dockerfile to build the image.

Mount the configuration directory as a writable volume to `/config`. The folder must be writable since the app will need
to store the list of discovered devices.

### Python

```bash
git clone https://github.com/Depau/meross2homie.git
cd meross2homie
pip install .

# To start the bridge
meross2homie
```

## Configuration

Place a `config.yml` file in the Docker configuration directory, or in the current working directory if you're running
it manually. Or pass it as a command line argument.

All settings are optional, except for the MQTT broker settings.

```yaml
# MQTT broker settings
mqtt_host: ""
mqtt_port: 1883
mqtt_username: ""         # Optional
mqtt_password: ""         # Optional
mqtt_clean_session: true  # Optional


# Meross MQTT protocol settings

# Key provided in the pairing app. It can be overridden on a per-device basis.
meross_key: ""                   # Optional

# Topic prefix that *this bridge* will see. Use a different value if you're
# mapping the devices to a different topic
meross_prefix: "/appliance"      # Optional

# Topic prefix that *the devices* will see. Defaults to the value of meross_prefix.
# Force this to "/appliance" if you're mapping the devices to a different topic.
meross_sent_prefix: "/appliance" # Optional

# Identifier used by this bridge. Change it if you're running multiple instances
# of this bridge.
meross_bridge_topic: "bridge"    # Optional


# Homie MQTT protocol settings

# Topic prefix for Homie devices. Defaults to "homie", before changing it ensure your
# Homie controller software is compliant and supports custom prefixes.
# openHAB is compliant and will work with any prefix.
homie_prefix: "homie"


# Bridge settings

# Path to the file where the list of discovered devices will be stored.
# Defaults to "devices.json" in the current working directory.
persistence_file: "devices.json" # Optional

# Device specific configuration. Use it to provide vanity names for the Homie devices,
# or to override the Meross key for a specific device. All the settings are optional.
devices:
  device_uuid:
    pretty_name: "Pretty name"
    pretty_topic: "pretty-topic"
    meross_key: "key"
  other_device_uuid:
  # ...
```

## MQTT server setup

Connecting the devices is tricky since they use their MAC address as username, and Mosquitto doesn't support `:` in
usernames.

To work around this, you can use the `auth_plugin` option in Mosquitto to authenticate the devices using a plugin.

The [Mosquitto YAML auth plugin](https://github.com/Depau/mosquitto_yaml_auth/) allows providing a YAML file with a list
of allowed users; see the README for more details.
I'm planning to make a plugin that authorizes Meross devices automatically.

You can use the `meross_mqtt_password` command installed by this package to generate the password for the devices.

```bash
# You can use an empty device key: "", and a random number as the user ID.
# You can obtain the MAC address from the pairing app.
$ meross_mqtt_password USER_ID MQTT_DEVICE_KEY 00:11:22:33:44:55
```

The username is the lowercase MAC address. The password is generated as follows:

```python
from hashlib import md5

password = str(user_id) + "_" + md5(mac_address + device_key).hexdigest().lower()
```

### Using a bridged broker

I recommend using a separate MQTT broker for the Meross devices, and bridging it to your main broker.

```
listener 8883

cafile /mosquitto/config/certs/ca.crt
certfile /mosquitto/config/certs/192.160.1.69.crt
keyfile /mosquitto/config/certs/192.160.1.69.key

allow_anonymous true
auth_plugin /mosquitto/config/libmosquitto_yaml_auth.so
auth_opt_users_file /mosquitto/config/users.yml
auth_opt_allow_anonymous true

connection bridge_to_main
address main_mosquitto:1883

# Maps /appliance/ to meross/
topic # both 2 /appliance/ meross/

# or if you don't want to do that
# topic /appliance/# both

bridge_attempt_unsubscribe false
remote_clientid broker.meross_bridge
try_private true
```

You can use [this script](https://gist.github.com/kirang89/b7579e5f331df2313078) to generate the TLS certificates:

```bash
./generate-CA.sh               # First generate the CA
./generate-CA.sh 192.168.1.69  # Generate certificates for broker IP address
```

Using TLS is required for the Meross devices to connect. The certificates must match the hostname configured on the
devices.

## Pairing to the custom broker

Download the [Custom Pairer](https://github.com/albertogeniola/Custom-Meross-Pairer/) Android app.

Open it and follow these steps to pair a new device:

1. Open the app
2. In the login page, select "Manual user/key setup"
3. Enter whatever user ID and user key you prefer
   I recommend:
  - User ID: your favorite number, or `0`
  - User key: leave it empty
1. Ignore the "API setup" page, just press "back"
2. Open the side drawer and select "Pair"
3. Grant the required permissions
4. Set the device to pairing mode by pressing and holding the button for a few seconds
5. Select the device from the list
6. The app will display some information about the device. It's a good time to copy the MAC address, go back to compute
   the password and add it to the MQTT broker `users.yml`.
7. Enter your Wi-Fi credentials
8. Enter the MQTT broker hostname and port
9. Start this bridge, so it can acknowledge the pairing request from the device.
