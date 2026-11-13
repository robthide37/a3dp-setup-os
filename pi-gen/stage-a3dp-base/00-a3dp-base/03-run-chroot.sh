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
chmod 0755 /usr/local/sbin/a3dp-remote-setup
chmod 0755 /usr/local/sbin/a3dp-monitor-restart
chmod 0755 /usr/local/sbin/a3dp-join
systemctl enable a3dp-firstboot.service

# ---- beta-collector detector runtime (baked; determinism over firstboot fetch) ----
# Pi 5 CPU ONNX detector for the data-collection kits: measured on the Rift's
# own Pi 5 (t_a922eefb) at mean 144 ms/frame, 119 MB RSS, 26/26 verdict parity
# with the deployed fp16 RKNN. onnxruntime pinned to the measured version;
# preprocessing is /255-normalized NCHW (the RKNN's raw 0-255 NHWC path
# saturates confidences — never port it; docs/pi5-onbox-detection.md).
python3 -m venv /opt/a3dp/detector-venv
/opt/a3dp/detector-venv/bin/pip install --no-cache-dir \
  "onnxruntime==1.31.1" "numpy<3" opencv-python-headless
chmod -R a+rX /opt/a3dp/detector-venv

# ---- beta-collector model artifact + digest gate ----
# The opset12+simplify export of the SAME checkpoint as the deployed fp16
# RKNN (provenance: docs/pi5-onbox-detection.md). sha256 sidecar is checked
# at build so a corrupted bake fails the image, not a kit at first boot.
install -d -m 0755 /opt/a3dp/models
( cd /opt/a3dp/models && sha256sum -c yolo11-failure-best-opset12.onnx.sha256 )

# ---- print-monitor key files: Mainsail-editable via symlinks (Rob-approved) ----
# The monitor/confirm-listener read /etc/a3dp-print-monitor/{llamaswap.key,
# confirm.token}; those are symlinks into ~/printer_data/config/a3dp/ so a
# customer pastes the two values with the Mainsail file manager (no SSH) and
# runs the RESTART_MONITOR macro. Perms preserved on the target files; nothing
# baked — the key files don't exist until the customer/support creates them.
install -d -o pi -g pi -m 0755 /home/pi/printer_data/config/a3dp
install -d -m 0755 /etc/a3dp-print-monitor
ln -sf /home/pi/printer_data/config/a3dp/llamaswap.key /etc/a3dp-print-monitor/llamaswap.key
ln -sf /home/pi/printer_data/config/a3dp/confirm.token /etc/a3dp-print-monitor/confirm.token

# ---- config backup agent (daily, 14 rotating snapshots) ----
cat > /usr/local/sbin/a3dp-config-backup <<'EOF'
#!/bin/bash
# Daily rotating backup of the klipper config tree (excl. firmware binaries).
# A profile may repoint the source/dest via /etc/a3dp-config-backup.conf
# (beta-collector backs up ~/kit-data minus frames instead).
set -u
SRC=/home/pi/printer_data/config
DST=/home/pi/printer_data/config_backups/agent
EXTRA_EXCLUDES=""
[ -f /etc/a3dp-config-backup.conf ] && . /etc/a3dp-config-backup.conf
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$DST"
tar czf "$DST/config-$STAMP.tar.gz" -C "$(dirname "$SRC")" "$(basename "$SRC")" \
    --exclude='firmware' --exclude='.theme' $EXTRA_EXCLUDES 2>/dev/null || exit 1
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

# Final ownership pass (katapult cloned as root above).
chown -R pi:pi /home/pi
