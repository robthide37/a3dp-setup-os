#!/bin/bash -e
# stage-a3dp-base / 01 — base system config.
# Folded from scripts/a3dp-host-install.sh §0-1 (live-validated 2026-10-31:
# timesync must be enabled via systemd-timesyncd only — `timedatectl set-ntp`
# needs polkit interaction and killed the first live run; fresh users need
# NOPASSWD sudo — fleet convention, added here once for all units).

# ---- timesync fix (known fleet pitfall: no working clock breaks forensics) ----
cat > /etc/systemd/timesyncd.conf <<'EOF'
[Time]
NTP=time.nist.gov pool.ntp.org
FallbackNTP=time.google.com
EOF
systemctl enable systemd-timesyncd

# ---- pi: passwordless sudo (fleet convention, LAN appliance; account itself
# stays LOCKED — no password is baked into the image, keys via Imager) ----
echo 'pi ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/010_pi-nopasswd
chmod 440 /etc/sudoers.d/010_pi-nopasswd

# ---- run-everything-as-pi home layout ----
install -d -o pi -g pi -m 0755 \
  /home/pi/printer_data/config /home/pi/printer_data/comms \
  /home/pi/printer_data/logs /home/pi/printer_data/gcodes
