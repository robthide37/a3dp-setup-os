#!/usr/bin/env bash
# a3dp-mcu-flashd installer — run ON the kit unit (Pi), as root:
#   sudo bash a3dp-mcu-flashd-install.sh
# Expects alongside this script (repo layout configs/meter-kit/a3dpmeter/):
#   a3dp-mcu-flash            (the flasher; usually scripts/a3dp-mcu-flash)
#   mcu-flashd.ini
#   mcu-flashd.service
#   nginx-a3dp-mcu-flash.conf
# Idempotent; safe to re-run. Does NOT restart klipper/moonraker (no dependency).
set -Eeuo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
for f in a3dp-mcu-flash mcu-flashd.ini mcu-flashd.service nginx-a3dp-mcu-flash.conf; do
  [[ -f "$here/$f" ]] || { echo "missing $here/$f"; exit 1; }
done

install -m 0755 "$here/a3dp-mcu-flash" /usr/local/bin/a3dp-mcu-flash
# Optional arg 1: ini variant path (default: this dir's meter-kit ini).
# Rift units: sudo bash a3dp-mcu-flashd-install.sh configs/rift/mcu-flashd.ini
INI="${1:-$here/mcu-flashd.ini}"
[[ -f "$INI" ]] || { echo "missing ini: $INI"; exit 1; }
install -m 0644 "$INI" /etc/a3dp-mcu-flashd.ini
install -m 0644 "$here/mcu-flashd.service" /etc/systemd/system/mcu-flashd.service
install -m 0644 "$here/nginx-a3dp-mcu-flash.conf" /etc/nginx/sites-available/a3dp-mcu-flash
ln -sf /etc/nginx/sites-available/a3dp-mcu-flash /etc/nginx/sites-enabled/a3dp-mcu-flash

# versioned firmware store (UI lists *.uf2 from here; no firmware shipped here)
FWD=/home/pi/printer_data/config/firmware
mkdir -p "$FWD"
if [[ ! -e "$FWD/README.md" ]]; then
cat > "$FWD/README.md" <<'EOF'
# Versioned MCU firmware store

Drop `.uf2` files named `<board>-<version>.uf2` here (e.g.
`rp2040main-v0.12.1.uf2`). `<board>` must match the section name in
`~/printer_data/config/meter-kit/mcus.ini` (rp2040main / rp2040head0).

- The web flasher (http://<pi>:8082) lists these files and flashes any board
  plugged in BOOTSEL mode (or sitting in the katapult bootloader) — works with
  klipper/moonraker down.
- NEVER blind-flash: match the version against klippy.log / the source tree
  first (runbook Step 4). This dir is the versioned source for recovery.
EOF
fi

# katapult bootloader flashing support (boards enumerating as usb-katapult_*:
# flashed via katapult's flashtool.py -d <by-id> -f <fw>, which needs pyserial)
if [[ ! -f /home/pi/katapult/scripts/flashtool.py ]]; then
  git clone --depth 1 https://github.com/Arksine/katapult /home/pi/katapult
fi
dpkg -s python3-serial >/dev/null 2>&1 || { apt-get update -qq; apt-get install -y python3-serial; }

# CAN transport support (katapult-over-CAN, e.g. Huvud/STM32 toolboards):
# enumeration + flashing shell out to canbus_query.py / katapult flashtool.py
# under can_python, which needs python-can. Optional: only checked when the
# ini sets can_interface to a real interface.
CANIF=$(python3 -c "import configparser;c=configparser.ConfigParser();c.read('$INI');print(c.get('service','can_interface',fallback='').strip())" 2>/dev/null || true)
if [[ -n "$CANIF" ]]; then
  CANPY=$(python3 -c "import configparser;c=configparser.ConfigParser();c.read('$INI');print(c.get('service','can_python',fallback='').strip())" 2>/dev/null || true)
  CANQ=$(python3 -c "import configparser;c=configparser.ConfigParser();c.read('$INI');print(c.get('service','canbus_query',fallback='').strip())" 2>/dev/null || true)
  if [[ -n "$CANPY" && -x "$CANPY" ]]; then
    "$CANPY" -c "import can" 2>/dev/null \
      || echo "WARNING: $CANPY lacks python-can — CAN enumeration/flashing will fail (install python-can into that venv)"
  else
    echo "WARNING: can_interface=$CANIF but can_python is missing/not executable in $INI"
  fi
  [[ -f "$CANQ" ]] || echo "WARNING: canbus_query script not found at $CANQ (set canbus_query or can_query_cmd in $INI)"
fi

python3 -c "import ast,sys; ast.parse(open('/usr/local/bin/a3dp-mcu-flash').read())"
/usr/local/bin/a3dp-mcu-flash enumerate --json >/dev/null  # smoke: module imports + enumerates

systemctl daemon-reload
systemctl enable --now mcu-flashd.service
nginx -t
systemctl reload nginx

echo "done. UI: http://$(hostname -I | awk '{print $1}'):8082"

# Mainsail button: sidebar navigation entry → this flasher UI. The BROWSER
# makes the call, so the trigger works with klippy (and moonraker) down —
# Mainsail macros need klippy and cannot be the klippy-independent surface.
THEME=/home/pi/printer_data/config/mainsail/.theme
mkdir -p "$THEME"
IP=$(hostname -I | awk '{print $1}')
python3 - "$THEME/navi.json" "http://$IP:8082/" <<'EOF'
import json, os, sys
path, href = sys.argv[1], sys.argv[2]
navi = []
if os.path.exists(path):
    try:
        navi = json.load(open(path))
    except Exception:
        navi = []
entry = {"title": "MCU Flash / Update", "href": href, "target": "_blank",
         "position": 95,
         "icon": "M7,2H13V9H17L11,22V13H7V2Z"}   # mdi flash
navi = [e for e in navi if e.get("title") != entry["title"]]
navi.append(entry)
json.dump(navi, open(path, "w"), indent=2)
print("mainsail button:", path)
EOF
