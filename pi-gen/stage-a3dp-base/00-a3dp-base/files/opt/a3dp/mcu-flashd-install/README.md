# a3dp-mcu-flashd — in-image redeploy kit

Self-contained copy of the flasher installer (printer-hardware
`configs/meter-kit/a3dpmeter/a3dp-mcu-flashd-install.sh` layout): the
installer expects its siblings side-by-side, so this dir carries all of them.

Contents:

| file | role |
|---|---|
| `a3dp-mcu-flashd-install.sh` | installer — idempotent, run as root: `sudo bash a3dp-mcu-flashd-install.sh [ini]` |
| `a3dp-mcu-flash` | the flasher payload (byte-identical to `scripts/a3dp-mcu-flash` on printer-hardware main) |
| `mcu-flashd.ini` | default ini variant (meter-kit layout, CAN DISABLED) |
| `mcu-flashd.rift.ini` | Rift variant (`flash/mcus.ini` layout, CAN DISABLED) |
| `mcu-flashd.service` | systemd unit |
| `nginx-a3dp-mcu-flash.conf` | nginx :8082 LAN-allowlist site |

The image bakes the same payload to `/usr/local/bin/a3dp-mcu-flash` +
`/etc/a3dp-mcu-flashd.ini` (default = the meter-kit variant) at build time;
this kit exists to REDEPLOY a different variant (or a payload update pulled
onto the unit) without re-flashing the SD.

## Per-machine CAN enablement (inert by default)

Shipped inis have `can_interface` EMPTY — the CAN transport (enumerate CAN
nodes, per-slot board picker, katapult-over-CAN flash) is compiled into the
payload but never activates. To enable on a CAN machine:

1. Set `can_interface = can0` (and confirm `can_python`/`canbus_query` paths)
   in the ini.
2. Ensure python-can is installed into the `can_python` venv.
3. Re-run the installer (it warns if the CAN prerequisites are missing).

Never enable CAN blind: the units that need it are known per-machine.
