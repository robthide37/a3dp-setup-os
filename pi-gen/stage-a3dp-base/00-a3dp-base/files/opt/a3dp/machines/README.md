# Machine profiles

First boot (`/usr/local/sbin/a3dp-firstboot`, log `/var/log/a3dp-firstboot.log`)
picks ONE profile by hostname prefix, or by `A3DP_MACHINE=<profile>` in
`/boot/firmware/a3dp-machine.conf` (boot-partition marker wins):

| Profile dir | Hostname match | Notes |
|---|---|---|
| `base/` | anything else | placeholder config only, no services |
| `rift-525/` | `rift*`, `a3dp525*` | live-verified a3dp525 config; bed-gate daemon enabled via `services.list` |
| `meter-idex/` | `meter*`, `a3dpmeter*` | meter kit config; MCU serials are `@A3DP_MCU_*@` tokens filled from `/dev/serial/by-id` at first boot (never guessed) |
| `beta-collector/` | `kit*`, `beta*` | data-collection kit (beta program, docs/beta-data-kit.md in print-monitoring): not a printer — the printer stack is switched off via `disable.list`; camera lane + `beta-kit-collector` only |
| `troodon-jake/` | `troodon*`, `a3dptr300*` | Jake's Troodon 300: dual-MCU (sam4e8e USB main + G0B1 CAN head0), Duex TMC2660×5/SX1509, Leadshine X/Y pulse-mode steppers, Euclid probe. Config = t_5351bc6a verified tree; deviations + decisions in its README (DangerKlipper→Kalico shaper regeneration is a migration step, not a first-boot one) |

A profile dir carries:

- `config/` — merged over the shared A3dp tree in `~/printer_data/config/`
  (profile wins on conflicts)
- `services.list` — one service name per line; each resolved from
  `/opt/a3dp/services/<name>/` (payload → `/home/pi/<name>`, unit → systemd,
  `nginx/*.conf` → sites-available + enabled). If the service dir ships a
  `<name>.timer`, the timer is enabled+started instead of starting the unit
  directly (timer-driven oneshot convention), and a `config.ini.example` in
  the payload is seeded as `config.ini` when none exists.
- `disable.list` — units disabled + stopped after the services install
  (beta-collector: klipper, moonraker, klipper-mcu, mcu-flashd, bedgate,
  nginx). Printer profiles ship an EMPTY list — no behavior change. The
  final nginx/moonraker/klipper restart block skips disabled units (a
  restart would re-START them).
- `restart.list` — units restarted once after the profile config merge (when
  the profile overwrote their live config; beta-collector restarts crowsnest
  for the 1080p MJPEG conf).
- `backup.conf` — optional repoint of the daily `a3dp-config-backup` agent
  (installed to `/etc/a3dp-config-backup.conf`; beta-collector backs up
  `~/kit-data` minus `frames/` instead of `printer_data/config`).
- optional `flash/`, `nginx/` — profile extras

Provenance: rift-525 config = live a3dp525 tree as captured during the
bed-gate deployment (t_1ce4ee85, live-verified 12/12); meter-idex config =
deployed machine config layer (t_4aaea5a4, live 2026-11-03) with chip-id
serials tokenized. Bench items (board rev, Y-endstop wiring, PRIM/SEC
assignment) stay per the runbook — the image does not guess them.
