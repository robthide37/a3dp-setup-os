#!/bin/bash
# a3dp-print-monitor — on-image installer (run ON the unit, needs sudo).
# The monitor ships BAKED but DISABLED at /opt/a3dp/monitor; this installs it
# into /home/pi/monitor + enables the units. It does NOT start the timer until
# a config exists with real endpoints, and the llama-swap API key / confirm
# token are NEVER baked (post-install secrets, /etc/a3dp-print-monitor/, 0600).
#
#   sudo bash /opt/a3dp/monitor/install-image.sh       # install, timer off
#   sudo systemctl enable --now a3dp-print-monitor.timer   # when configured
set -euo pipefail
SRC_DIR=/opt/a3dp/monitor
MON_DIR=/home/pi/monitor
VENV=/home/pi/monitor/venv
PM=/home/pi/print-monitor

mkdir -p "$MON_DIR" "$PM/frames" "$PM/logs" "$PM/alerts/processed" "$PM/pause-frames"
chown -R pi:pi "$MON_DIR" "$PM"
install -o pi -g pi -m 644 "$SRC_DIR/a3dp_print_monitor.py" "$MON_DIR/"
install -o pi -g pi -m 644 "$SRC_DIR/confirm_listener.py" "$MON_DIR/"
[ -f "$MON_DIR/config.ini" ] || install -o pi -g pi -m 600 "$SRC_DIR/config.ini.example" "$MON_DIR/config.ini"
[ -f "$MON_DIR/confirm-listener.ini" ] || install -o pi -g pi -m 600 "$SRC_DIR/confirm-listener.ini.example" "$MON_DIR/confirm-listener.ini"

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet numpy opencv-python-headless
fi

install -m 0644 "$SRC_DIR/a3dp-print-monitor.service" /etc/systemd/system/
install -m 0644 "$SRC_DIR/a3dp-print-monitor.timer"   /etc/systemd/system/
install -m 0644 "$SRC_DIR/a3dp-confirm-listener.service" /etc/systemd/system/
systemctl daemon-reload
# staged units ship DISABLED: enable the timer only after config.ini points at
# a real snapshot/moonraker endpoint and /etc/a3dp-print-monitor/llamaswap.key
# exists (0600). Confirm listener: same, plus confirm.token.
echo "installed. config: $MON_DIR/config.ini (edit endpoints + rotate180 flag)"
echo "secrets (post-install): /etc/a3dp-print-monitor/{llamaswap.key,confirm.token}"
echo "enable: sudo systemctl enable --now a3dp-print-monitor.timer"
