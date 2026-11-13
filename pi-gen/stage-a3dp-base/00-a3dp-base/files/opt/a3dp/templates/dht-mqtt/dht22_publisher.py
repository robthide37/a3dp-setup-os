#!/usr/bin/env python3
"""DHT22 temperature/humidity -> MQTT publisher for Moonraker [sensor] display.

Publishes a retained JSON payload {"temp": <degC>, "hum": <pct>} to the topic
`sensors/<name>` every --interval seconds. Moonraker's [sensor] component
(type: mqtt, state_topic: sensors/<name>) picks it up and Mainsail displays it.

Two read sources:
  --source gpio  DHT22 bit-banged on a GPIO via adafruit-circuitpython-dht
                 (Blinka). NOTE: on Debian trixie the system libgpiod is 2.x
                 and Blinka targets the 1.x API; if this source fails at
                 startup or read time, use the kernel-driver fallback instead.
  --source iio   Kernel driver (dtoverlay=dht11,gpiopin=N in /boot/firmware/
                 config.txt) exposing an IIO device. Reads in_temp_input
                 (milli-degC) and in_humidityrelative_input (milli-%).

Transient read failures (checksum, timeout, missing hardware) NEVER exit the
process: the error is logged once, then retried on the next cycle. Only a
publish loop that cannot be recovered kills the service (systemd Restart=always
covers even that).

Journal-friendly: plain single-line log records on stdout/stderr; systemd
forwards them to the journal, so do not add color or multiline records.
"""

import argparse
import json
import logging
import os
import signal
import sys
import time

LOG = logging.getLogger("dht22-publisher")

EXIT_OK = 0
EXIT_CONFIG = 2

# DHT22 datasheet: do not poll faster than one read per ~2 s.
MIN_INTERVAL_S = 3.0


# --------------------------------------------------------------------------
# Sensor sources: read() -> (temp_c: float, hum_pct: float) or None on failure
# --------------------------------------------------------------------------

class GpioSource:
    """Bit-banged DHT22 via adafruit-circuitpython-dht / Blinka."""

    def __init__(self, gpio: int):
        import adafruit_dht
        import board

        pin = getattr(board, "D%d" % gpio, None)
        if pin is None:
            raise ValueError("board has no D%d pin object" % gpio)
        self._dht = adafruit_dht.DHT22(pin)
        self._gpio = gpio

    def read(self):
        t = self._dht.temperature
        h = self._dht.humidity
        if t is None or h is None:
            return None
        return (float(t), float(h))


class IioSource:
    """Kernel IIO device (dtoverlay=dht11,gpiopin=N)."""

    def __init__(self, gpio: int, iio_device: str):
        self._temp_path = os.path.join(iio_device, "in_temp_input")
        self._hum_path = os.path.join(iio_device, "in_humidityrelative_input")
        missing = [p for p in (self._temp_path, self._hum_path) if not os.path.exists(p)]
        if missing:
            raise ValueError(
                "IIO device %s lacks %s; is 'dtoverlay=dht11,gpiopin=%d' set "
                "in /boot/firmware/config.txt and the Pi rebooted?"
                % (iio_device, ", ".join(missing), gpio)
            )
        self._gpio = gpio

    @staticmethod
    def _read_milli(path):
        with open(path) as f:
            return float(f.read().strip())

    def read(self):
        temp_c = self._read_milli(self._temp_path) / 1000.0
        hum = self._read_milli(self._hum_path) / 1000.0
        return (temp_c, hum)


def make_source(args):
    if args.source == "gpio":
        return GpioSource(args.gpio)
    device = args.iio_device or "/sys/bus/iio/devices/iio:device0"
    return IioSource(args.gpio, device)


# --------------------------------------------------------------------------
# MQTT
# --------------------------------------------------------------------------

def make_mqtt_client(host, port, name):
    import paho.mqtt.client as mqtt

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="dht22-%s" % name,
    )
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(host, port, keepalive=60)
    client.loop_start()
    return client


def publish(client, topic, temp_c, hum_pct):
    payload = json.dumps({"temp": round(temp_c, 1), "hum": round(hum_pct, 1)})
    info = client.publish(topic, payload, qos=1, retain=True)
    info.wait_for_publish(timeout=10)
    if not info.is_published():
        raise TimeoutError("broker did not ack publish")


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

_running = True


def _stop(_sig, _frame):
    global _running
    _running = False


def main(argv=None):
    ap = argparse.ArgumentParser(description="DHT22 temperature/humidity -> MQTT publisher for Moonraker [sensor] display.")
    ap.add_argument("--name", required=True,
                    help="sensor name; publishes to sensors/<name>")
    ap.add_argument("--gpio", type=int, required=True,
                    help="BCM GPIO number the DHT22 data line is on")
    ap.add_argument("--source", choices=("gpio", "iio"), default="gpio",
                    help="read source: bit-banged GPIO (Blinka) or kernel IIO device")
    ap.add_argument("--mqtt-host", default="localhost")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--interval", type=float, default=10.0,
                    help="seconds between reads/publishes (min %.0f)" % MIN_INTERVAL_S)
    ap.add_argument("--iio-device", default=None,
                    help="IIO device dir for --source iio (default "
                         "/sys/bus/iio/devices/iio:device0)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    if args.interval < MIN_INTERVAL_S:
        LOG.warning("--interval %.1f below DHT22 minimum; clamping to %.0f s",
                    args.interval, MIN_INTERVAL_S)
        args.interval = MIN_INTERVAL_S

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    topic = "sensors/%s" % args.name

    try:
        source = make_source(args)
    except Exception:
        LOG.exception("failed to initialize %s source for GPIO%d — not starting loop",
                      args.source, args.gpio)
        return EXIT_CONFIG

    client = make_mqtt_client(args.mqtt_host, args.mqtt_port, args.name)
    LOG.info("publishing %s -> mqtt://%s:%d/%s (source=%s, gpio=%d, interval=%.0fs)",
             args.name, args.mqtt_host, args.mqtt_port, topic,
             args.source, args.gpio, args.interval)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    last_err = None
    err_count = 0
    while _running:
        started = time.monotonic()
        try:
            reading = source.read()
            if reading is None:
                raise RuntimeError("sensor returned None (no data)")
            temp_c, hum_pct = reading
            if not (0.0 <= hum_pct <= 100.0) or not (-80.0 <= temp_c <= 120.0):
                raise ValueError("implausible reading t=%.1f h=%.1f" % (temp_c, hum_pct))
            publish(client, topic, temp_c, hum_pct)
            if last_err is not None:
                LOG.info("recovered after %d failed read(s): t=%.1fC h=%.1f%%",
                         err_count, temp_c, hum_pct)
            last_err, err_count = None, 0
        except Exception as exc:
            # Transient failures (checksum, timeout, unwired sensor, broker
            # hiccup) must never exit the process. Log once per distinct
            # error, then every 60th repeat to keep the journal quiet.
            msg = "%s: %s" % (type(exc).__name__, exc)
            if msg != last_err:
                LOG.warning("read/publish failed (%s) — retrying next cycle", msg)
                last_err, err_count = msg, 1
            else:
                err_count += 1
                if err_count % 60 == 0:
                    LOG.warning("still failing after %d cycles: %s", err_count, msg)
        # Interruptible sleep so 'systemctl stop' is prompt.
        while _running and time.monotonic() - started < args.interval:
            time.sleep(0.25)

    client.loop_stop()
    LOG.info("stopped")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
