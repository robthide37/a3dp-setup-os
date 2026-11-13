#!/bin/bash -e
# stage-a3dp-base / 02 — klipper stack: Kalico, Moonraker, Mainsail+nginx,
# Crowsnest, klipper host-mcu.
# Folded from scripts/a3dp-host-install.sh §2-5b. Live-validated deviations kept:
#  - klipper.service rewritten to printer_data layout + explicit -a UDS socket
#    (Kalico does NOT default the API socket — klippy ready but moonraker never
#    connected without it, live 2026-10-31)
#  - Crowsnest installed UNATTENDED; its installer exits 1 after installing
#    everything (CROWSNEST_CONFIG_PATH empty under set -Ee) — tolerated, the
#    moonraker update entry comes from the canonical moonraker.conf instead
#  - empty printer.cfg is invalid on Kalico — placeholder config shipped
export HOME=/home/pi
KLIPPER_PIN="84b3e373"          # fleet protocol pin: v2026.10.00-177-g84b3e373

# ---- Kalico (fleet standard) ----
git clone https://github.com/KalicoCrew/kalico.git /home/pi/klipper
git -C /home/pi/klipper checkout --detach "$KLIPPER_PIN"
cd /home/pi/klipper
./scripts/install-debian.sh || true
# install-debian.sh may pull python deps as the invoking user; venv must be pi's
[ -x /home/pi/klippy-env/bin/python ] || python3 -m venv /home/pi/klippy-env
/home/pi/klippy-env/bin/pip install -r /home/pi/klipper/scripts/klippy-requirements.txt

# Rewrite the stock klipper.service (printer_data layout + -a socket).
cat > /etc/systemd/system/klipper.service <<'EOF'
# A3DP fleet klipper.service — printer_data layout, explicit API socket.
# (Stock Kalico unit uses ~/printer.cfg + /tmp log + no -a; moonraker never
# connected — live-verified 2026-10-31.)
[Unit]
Description=Starts klippy on startup
After=network.target

[Install]
WantedBy=multi-user.target

[Service]
Type=simple
User=pi
RemainAfterExit=yes
# Rob-approved performance defaults (t_9cca9150): dedicated core + elevated
# priority for klippy so motion timing never competes with nginx/monitor/
# tailscale; verified target = zero "Timer too close" warnings on a test
# print (bench-flash check, open in runbooks/a3dp-image-pipeline.md).
CPUAffinity=3
Nice=-10
WorkingDirectory=/home/pi/klipper
ExecStart=/home/pi/klippy-env/bin/python /home/pi/klipper/klippy/klippy.py /home/pi/printer_data/config/printer.cfg -a /home/pi/printer_data/comms/klippy.sock -l /home/pi/printer_data/logs/klippy.log
Restart=always
RestartSec=10
EOF

# Placeholder config (empty config is invalid — klippy always needs [mcu]).
cat > /home/pi/printer_data/config/printer.cfg <<'EOF'
# A3DP placeholder config — replaced at first boot by the machine profile
# (see /usr/local/sbin/a3dp-firstboot). Safe state: no motion, no heaters.
[mcu]
serial: /dev/null

[printer]
kinematics: none
max_velocity: 1
max_accel: 1
EOF

# ---- Moonraker (pinned for image determinism) ----
MOONRAKER_PIN="b559b9a81161f958a43c2f527b6ad96f73310d5d"
git clone https://github.com/Arksine/moonraker.git /home/pi/moonraker
git -C /home/pi/moonraker checkout --detach "$MOONRAKER_PIN"
cd /home/pi/moonraker
# install-moonraker.sh refuses root (it would create the pi service/dirs);
# the chroot build is root, so units/dirs are laid out by this stage instead.
./scripts/install-moonraker.sh || true
[ -x /home/pi/moonraker-env/bin/python ] || python3 -m venv /home/pi/moonraker-env
/home/pi/moonraker-env/bin/pip install -r /home/pi/moonraker/scripts/moonraker-requirements.txt

# Canonical fleet moonraker.conf (replaces the generated one — the generated
# conf lacks [authorization] entirely: localhost got 401, live 2026-10-31).
install -o pi -g pi -m 0644 /opt/a3dp/shared/moonraker.conf \
  /home/pi/printer_data/config/moonraker.conf

# moonraker.service: installed here, not by install-moonraker.sh (refuses
# root). Same printer_data layout + venv conventions as klipper.service above.
cat > /etc/systemd/system/moonraker.service <<'EOF'
# A3DP fleet moonraker.service — printer_data layout.
[Unit]
Description=Moonraker API server for klippy
After=network-online.target klipper.service
Wants=network-online.target

[Install]
WantedBy=multi-user.target

[Service]
Type=simple
User=pi
RemainAfterExit=yes
WorkingDirectory=/home/pi/moonraker
ExecStart=/home/pi/moonraker-env/bin/python /home/pi/moonraker/moonraker/moonraker.py -c /home/pi/printer_data/config/moonraker.conf -l /home/pi/printer_data/logs/moonraker.log
Restart=always
RestartSec=10
EOF

# ---- moonraker components: mcu_flasher + timelapse ----
git clone https://github.com/juliapythoncpp/moonraker-mcu-flasher.git /home/pi/moonraker-mcu-flasher
ln -sf /home/pi/moonraker-mcu-flasher/moonraker/mcu_flasher.py \
  /home/pi/moonraker/moonraker/components/mcu_flasher.py

git clone https://github.com/mainsail-crew/moonraker-timelapse.git /home/pi/moonraker-timelapse
if [ -f /home/pi/moonraker-timelapse/component/timelapse.py ]; then
  ln -sf /home/pi/moonraker-timelapse/component/timelapse.py \
    /home/pi/moonraker/moonraker/components/timelapse.py
elif [ -f /home/pi/moonraker-timelapse/moonraker/components/timelapse.py ]; then
  ln -sf /home/pi/moonraker-timelapse/moonraker/components/timelapse.py \
    /home/pi/moonraker/moonraker/components/timelapse.py
fi
[ -f /home/pi/moonraker-timelapse/requirements.txt ] && \
  /home/pi/moonraker-env/bin/pip install -r /home/pi/moonraker-timelapse/requirements.txt || true

# ---- klipper host-mcu (Rift [mcu rpi]) ----
cd /home/pi/klipper/scripts
make || true
if [ -f klipper-mcu ]; then
  install -m 0755 klipper-mcu /usr/local/bin/klipper_mcu
  install -m 0644 klipper-mcu.service /etc/systemd/system/klipper-mcu.service
  systemctl enable klipper-mcu.service
fi

# ---- Mainsail + nginx ----
cd /home/pi
MAINSAIL_URL=$(curl -s https://api.github.com/repos/mainsail-crew/mainsail/releases/latest \
  | grep browser_download_url | grep 'mainsail.zip' | cut -d '"' -f 4)
rm -rf /var/www/html
mkdir -p /var/www/html
wget -qO /tmp/mainsail.zip "$MAINSAIL_URL"
unzip -o /tmp/mainsail.zip -d /var/www/html
ln -sf /etc/nginx/sites-available/mainsail /etc/nginx/sites-enabled/mainsail
rm -f /etc/nginx/sites-enabled/default

# ---- Crowsnest (camera) — UNATTENDED; exit-1 after successful install is a
# known deviation (see file header), tolerated. ----
# Installer runs as pi via inner sudo (live-validated flow, runbook #5):
#   pi$ sudo CROWSNEST_UNATTENDED=1 make install  -> SUDO_USER=pi passes the
# installer's not-as-root guard. Direct root run or `sudo -u pi make` sets
# SUDO_USER=root/unset and aborts (CI failures 2026-11-13).
# Root-run installers/venvs above leave root-owned dirs under /home/pi;
# crowsnest creates ~/printer_data/systemd AS PI and dies on root-owned dirs
# (CI failure 2026-11-13). Normalize before it runs.
chown -R pi:pi /home/pi
git clone https://github.com/mainsail-crew/crowsnest.git /home/pi/crowsnest
chown -R pi:pi /home/pi/crowsnest
sudo -u pi bash -c "cd /home/pi/crowsnest && \
  sudo CROWSNEST_UNATTENDED=1 DEBIAN_FRONTEND=noninteractive make install" || \
  echo "NOTE: crowsnest installer exit code tolerated (known unattended deviation)"

# Crowsnest is required camera infrastructure: its service MUST exist.
[ -f /etc/systemd/system/crowsnest.service ] || {
  echo "FATAL: crowsnest.service missing after unattended install"; exit 1; }

systemctl enable klipper.service moonraker.service nginx.service

# ---- host performance defaults (t_9cca9150, Rob-approved direction) ----
# Camera stack + AI work nice'd down (klipper.service above gets the core +
# Nice=-10). No SCHED_FIFO. No swap on SD-card units: SD swap ruins motion
# timing and wears the card; zram is the follow-up if a unit ever needs it.
mkdir -p /etc/systemd/system/crowsnest.service.d
cat > /etc/systemd/system/crowsnest.service.d/10-a3dp-nice.conf <<'EOF'
# A3DP: keep the camera stack out of klippy's way (t_9cca9150)
[Service]
Nice=10
CPUWeight=40
EOF
systemctl disable --now dphys-swapfile >/dev/null 2>&1 || true
systemctl mask dphys-swapfile >/dev/null 2>&1 || true

# Final ownership pass: root-run clones/installers above must end up pi-owned
# (update managers, monitor, bedgate all run as pi).
chown -R pi:pi /home/pi
