#!/bin/bash -e
# stage-a3dp-base / 03 — A3DP layer: katapult, mcu-flashd, shared A3dp config
# tree, first-boot activation machinery, config backup agent.
# Folded from scripts/a3dp-host-install.sh §4b + configs/meter-kit/a3dpmeter/
# (a3dp-mcu-flashd installer) + runbooks/meter-kit-image.md lessons.

# ---- katapult (meter toolboards boot through it) + pyserial (in 00-packages) ----
git clone https://github.com/Arksine/katapult.git /home/pi/katapult

# ---- a3dp-mcu-flashd (Klipper-independent recovery flasher, :8082) ----
# Files were placed by the stage files/ tree; wire them up.
chmod 0755 /usr/local/bin/a3dp-mcu-flash
ln -sf /etc/nginx/sites-available/a3dp-mcu-flash /etc/nginx/sites-enabled/a3dp-mcu-flash
install -d -o pi -g pi -m 0755 /home/pi/printer_data/config/firmware
if [ ! -e /home/pi/printer_data/config/firmware/README.md ]; then
cat > /home/pi/printer_data/config/firmware/README.md <<'EOF'
# Versioned MCU firmware store

Drop `.uf2` files named `<board>-<version>.uf2` here. `<board>` must match the
section name in `~/printer_data/config/meter-kit/mcus.ini`. The web flasher
(http://<pi>:8082) lists these files and flashes any board plugged in BOOTSEL
mode (or sitting in the katapult bootloader) — works with klipper/moonraker
down. NEVER blind-flash: match the version against klippy.log / the source tree
first.
EOF
fi
systemctl enable mcu-flashd.service

# ---- shared A3dp config tree (update-managed; pinned for image determinism) ----
git clone https://github.com/robthide37/A3dp.git /opt/a3dp/shared/A3dp
git -C /opt/a3dp/shared/A3dp checkout --detach 286534f
rm -rf /opt/a3dp/shared/A3dp/.git

# ---- first-boot machine activation ----
chmod 0755 /usr/local/sbin/a3dp-firstboot
chmod 0755 /usr/local/sbin/a3dp-client-ui
systemctl enable a3dp-firstboot.service

# ---- config backup agent (daily, 14 rotating snapshots) ----
cat > /usr/local/sbin/a3dp-config-backup <<'EOF'
#!/bin/bash
# Daily rotating backup of the klipper config tree (excl. firmware binaries).
set -u
SRC=/home/pi/printer_data/config
DST=/home/pi/printer_data/config_backups/agent
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$DST"
tar czf "$DST/config-$STAMP.tar.gz" -C "$(dirname "$SRC")" "$(basename "$SRC")" \
    --exclude='firmware' --exclude='.theme' 2>/dev/null || exit 1
ls -1t "$DST"/config-*.tar.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
EOF
chmod 0755 /usr/local/sbin/a3dp-config-backup
cat > /etc/systemd/system/a3dp-config-backup.service <<'EOF'
[Unit]
Description=A3DP config backup (daily snapshot of printer_data/config)

[Service]
Type=oneshot
User=pi
ExecStart=/usr/local/sbin/a3dp-config-backup
EOF
cat > /etc/systemd/system/a3dp-config-backup.timer <<'EOF'
[Unit]
Description=A3DP config backup timer (daily)

[Timer]
OnCalendar=*-*-* 03:17:00
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl enable a3dp-config-backup.timer

# ---- ownership: everything under /home/pi is pi's ----
chown -R pi:pi /home/pi/printer_data /home/pi/klipper /home/pi/moonraker \
  /home/pi/moonraker-mcu-flasher /home/pi/moonraker-timelapse \
  /home/pi/crowsnest /home/pi/katapult 2>/dev/null || true
chown -R pi:pi /home/pi/moonraker-env /home/pi/klippy-env 2>/dev/null || true
