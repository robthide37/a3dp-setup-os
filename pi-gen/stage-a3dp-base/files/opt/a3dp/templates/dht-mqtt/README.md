# DHT22 drybox monitoring via MQTT + Moonraker [sensor]

Data-only humidity/temperature for dryboxes. A small Python publisher reads a
DHT22 on a host GPIO (or a kernel IIO device) and publishes retained JSON to
`mosquitto` on localhost; Moonraker's `[sensor]` component subscribes and the
value shows up in Mainsail's Temperature panel ("Sensors" section).

**Scope guard:** data display only. No alerts, no macros, and never any
Klipper config changes — Klipper has no DHT support (`klippy/extras/dht.py`
does not exist in mainline) and none is planned.

## Files

| File | Purpose |
|---|---|
| `dht22_publisher.py` | Publisher daemon: `--name --gpio [--source gpio\|iio] [--mqtt-host localhost] [--mqtt-port 1883] [--interval 10] [--iio-device PATH]`. Publishes retained `{"temp": <°C>, "hum": <%>}` to `sensors/<name>`. Never exits on transient read errors. |
| `dht22-publisher@.service` | systemd template; `%i` = sensor name (e.g. `drybox`). Per-instance GPIO goes in `/etc/dht-mqtt/<name>.conf`. |
| `moonraker-sensor.conf.example` | `[mqtt]` + `[sensor drybox]` (+ commented `[sensor chamber]`) blocks to append to `moonraker.conf`. |

## Pin map (meter kit / a3dpmeter, Pi 5 header)

- Drybox DHT22: data **GPIO17** (physical pin 11), VCC **3.3 V** (pin 1), GND (pin 9)
- Chamber DHT22 (documented, NOT enabled): data **GPIO4** (physical pin 7)
- **GPIO13 is reserved** — both Y endstops. Do not use.

The DHT22 data line needs a pull-up (4.7–10 kΩ to 3.3 V); most three-pin
carrier modules include one.

## Per-machine install

### 1. Broker

```bash
sudo apt-get install -y mosquitto mosquitto-clients
# Mosquitto 2.x default = localhost-only listener on :1883. Verify:
ss -tlnp | grep 1883   # must show 127.0.0.1 or ::1 ONLY
```

### 2. Publisher

```bash
sudo mkdir -p /opt/dht-mqtt /etc/dht-mqtt
sudo cp dht22_publisher.py /opt/dht-mqtt/
sudo cp dht22-publisher@.service /etc/systemd/system/
# venv (Debian 12+/trixie is PEP 668 externally-managed — never system pip)
sudo python3 -m venv /opt/dht-mqtt/venv
sudo /opt/dht-mqtt/venv/bin/pip install paho-mqtt adafruit-circuitpython-dht "gpiod>=2.2.2,<3"
sudo printf 'GPIO=17\n' | sudo tee /etc/dht-mqtt/drybox.conf
sudo systemctl daemon-reload
sudo systemctl enable --now dht22-publisher@drybox
journalctl -u dht22-publisher@drybox -f
```

Without hardware attached the service must stay `active (running)` and log
repeating `read/publish failed` warnings — that is the correct no-wire state.
If instead it exits with an import/init error, the service is misconfigured.

### 3. Moonraker

```bash
cp ~/printer_data/config/moonraker.conf ~/printer_data/config/moonraker.conf.pre-dht
# append [mqtt] + [sensor drybox] from moonraker-sensor.conf.example, then:
sudo systemctl restart moonraker
```

### 4. Verify (works before any hardware is wired — simulated payload)

```bash
mosquitto_pub -h localhost -t sensors/drybox -m '{"temp": 24.5, "hum": 40.0}' -r
curl -s localhost:7125/server/sensors/list
# -> {"result":{"sensors":{"drybox":{...,"values":{"temperature":24.5,"humidity":40.0}}}}}
curl -s localhost:7125/server/sensors/measurements
# -> {"result":{"drybox":{"temperature":[24.5,...],"humidity":[40.0,...]}}}
```

Verified endpoint set on current Moonraker (b559b9a): `/server/sensors/list`,
`/server/sensors/info?sensor=<id>`, `/server/sensors/measurements`. There is
no `/server/sensors/sensors_data` route.

The publisher's MQTT half is separately self-testable with no hardware:

```bash
sudo /opt/dht-mqtt/venv/bin/python -c "
import sys, time; sys.path.insert(0, '/opt/dht-mqtt')
from dht22_publisher import make_mqtt_client, publish
c = make_mqtt_client('localhost', 1883, 'selftest'); time.sleep(1)
publish(c, 'sensors/selftest', 23.7, 41.2); print('publish OK'); c.loop_stop()"
mosquitto_sub -h localhost -t sensors/selftest -C 1   # -> {"temp": 23.7, "hum": 41.2}
mosquitto_pub -h localhost -t sensors/selftest -n -r  # clear the retained msg
```

In Mainsail: Temperature panel → "Sensors" section shows Drybox with both
values. The retained payload also means Moonraker shows the last reading
immediately after any restart.

## gpiod 2.x pitfall (Debian trixie) and the IIO fallback

trixie ships **libgpiod 2.x**, whose Python API differs from 1.x. RESOLVED on
a3dpmeter (Pi 5, trixie, Python 3.13.5): Blinka 10.2.0 + pip `gpiod 2.6.0`
works — install `gpiod>=2.2.2,<3` into the venv (Blinka requires it and will
refuse to `import board` without it; the system `python3-libgpiod` package is
NOT visible inside a venv). Verified: `adafruit_dht.DHT22(board.D17)`
instantiates; with no module wired the read raises
`RuntimeError: DHT sensor not found, check wiring`, which the publisher logs
once and retries. Keep the fallback below in your back pocket anyway:

Fallback: kernel driver, no Blinka involved.

1. `/boot/firmware/config.txt`: add `dtoverlay=dht11,gpiopin=17`, reboot.
2. Find the device: `ls /sys/bus/iio/devices/` → e.g. `iio:device0` with
   `in_temp_input` (milli-°C) and `in_humidityrelative_input` (milli-%).
3. `/etc/dht-mqtt/drybox.conf`:
   `EXTRA_ARGS=--source iio --iio-device /sys/bus/iio/devices/iio:device0`
4. `sudo systemctl restart dht22-publisher@drybox`

Last resort (no GPIO at all): an RP2040 Pico dongle reads the DHT22 and emits
JSON over USB-CDC; a small serial→MQTT bridge can reuse this publisher's MQTT
half. Documented only — not implemented here.

## Enabling a second sensor (e.g. chamber)

1. Wire it (chamber: GPIO4, pin 7) and confirm the publisher instance works:
   `printf 'GPIO=4\n' | sudo tee /etc/dht-mqtt/chamber.conf`,
   `sudo systemctl enable --now dht22-publisher@chamber`.
2. Uncomment the `[sensor chamber]` block in `moonraker.conf`, restart
   Moonraker.
3. Never enable the Moonraker sensor before its publisher publishes — a
   sensor with no live publisher just serves a stale retained value.
