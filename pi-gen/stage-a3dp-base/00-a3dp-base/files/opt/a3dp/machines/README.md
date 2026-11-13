# Machine profiles

First boot (`/usr/local/sbin/a3dp-firstboot`, log `/var/log/a3dp-firstboot.log`)
picks ONE profile by hostname prefix, or by `A3DP_MACHINE=<profile>` in
`/boot/firmware/a3dp-machine.conf` (boot-partition marker wins):

| Profile dir | Hostname match | Notes |
|---|---|---|
| `base/` | anything else | placeholder config only, no services |
| `rift-525/` | `rift*`, `a3dp525*` | live-verified a3dp525 config; bed-gate daemon enabled via `services.list` |
| `meter-idex/` | `meter*`, `a3dpmeter*` | meter kit config; MCU serials are `@A3DP_MCU_*@` tokens filled from `/dev/serial/by-id` at first boot (never guessed) |

A profile dir carries:

- `config/` — merged over the shared A3dp tree in `~/printer_data/config/`
  (profile wins on conflicts)
- `services.list` — one service name per line; each resolved from
  `/opt/a3dp/services/<name>/` (payload → `/home/pi/<name>`, unit → systemd,
  `nginx/*.conf` → sites-available + enabled)
- optional `flash/`, `nginx/` — profile extras

Provenance: rift-525 config = live a3dp525 tree as captured during the
bed-gate deployment (t_1ce4ee85, live-verified 12/12); meter-idex config =
deployed machine config layer (t_4aaea5a4, live 2026-11-03) with chip-id
serials tokenized. Bench items (board rev, Y-endstop wiring, PRIM/SEC
assignment) stay per the runbook — the image does not guess them.
