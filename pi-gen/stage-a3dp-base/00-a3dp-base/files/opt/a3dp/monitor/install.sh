#!/bin/bash
# Install/upgrade a3dp-print-monitor + confirm listener on a3dpmeter
# (run ON the Pi, needs sudo). Idempotent. Assumes:
#   /home/pi/monitor/a3dp_print_monitor.py    (copied from repo monitor/)
#   /home/pi/monitor/confirm_listener.py      (copied from repo monitor/)
#   /home/pi/monitor/config.ini               (copied from config.ini.example)
#   /home/pi/monitor/confirm-listener.ini     (copied from confirm-listener.ini.example)
#   /etc/a3dp-print-monitor/llamaswap.key     (0600, llama-swap API key — separate step)
set -euo pipefail
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
MON_DIR=/home/pi/monitor
VENV=/home/pi/monitor-rnd/venv   # reuse the R&D venv (numpy + opencv-headless)
PM=/home/pi/print-monitor

mkdir -p "$MON_DIR" "$PM/frames" "$PM/logs" "$PM/alerts/processed" "$PM/pause-frames"
chown pi:pi "$MON_DIR" "$PM" "$PM/frames" "$PM/logs" "$PM/alerts" "$PM/alerts/processed" "$PM/pause-frames"
install -o pi -g pi -m 644 "$SRC_DIR/a3dp_print_monitor.py" "$MON_DIR/"
install -o pi -g pi -m 644 "$SRC_DIR/confirm_listener.py" "$MON_DIR/"
[ -f "$MON_DIR/config.ini" ] || install -o pi -g pi -m 600 "$SRC_DIR/config.ini.example" "$MON_DIR/config.ini"
[ -f "$MON_DIR/confirm-listener.ini" ] || install -o pi -g pi -m 600 "$SRC_DIR/confirm-listener.ini.example" "$MON_DIR/confirm-listener.ini"

# venv: reuse R&D venv if present, else create
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet numpy opencv-python-headless
fi
ln -sfn "$VENV" "$MON_DIR/venv"

# shared confirm token (OP5 detector + this listener). Generated once; the
# same token must be copied to the OP5 at /etc/a3dp-print-monitor/confirm.token
install -d -o root -g pi -m 750 /etc/a3dp-print-monitor
if [ ! -f /etc/a3dp-print-monitor/confirm.token ]; then
  openssl rand -hex 32 | install -o root -g pi -m 640 /dev/stdin /etc/a3dp-print-monitor/confirm.token
  echo "generated /etc/a3dp-print-monitor/confirm.token — COPY IT TO THE OP5"
fi

install -m 644 "$SRC_DIR/a3dp-print-monitor.service" "$SRC_DIR/a3dp-print-monitor.timer" \
  "$SRC_DIR/a3dp-confirm-listener.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now a3dp-print-monitor.timer
systemctl enable --now a3dp-confirm-listener.service
echo "--- timer:"
systemctl list-timers a3dp-print-monitor.timer --no-pager
echo "--- listener:"
systemctl is-active a3dp-confirm-listener.service
echo "installed. test one cycle: sudo systemctl start a3dp-print-monitor.service && journalctl -u a3dp-print-monitor -n 20"
echo "listener health: curl -s http://127.0.0.1:8765/health"
