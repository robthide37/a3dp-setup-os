# rift-525 machine overlay

Deployable config for fresh flashes (a3dp-image). Since 2026-11-15 (t_a0980f4a)
this uses the include-based layout: thin `config/printer.cfg` manifest +
`heaters.cfg`/`motion.cfg`/`sensors.cfg`/`fans.cfg` + `macros/`.

The full file map and edit rules live in the printer-hardware repo:
`configs/rift/README.md`. This tree is byte-identical to that repo tree except:

- `[include macros/a3dp-remote.cfg]` in printer.cfg (remote-access macros,
  overlay-only; backed by gcode_shell_command sections in a3dp-remote.cfg)
- printer.cfg carries the reference unit's SAVE_CONFIG block as family
  defaults for first boot (live machines keep their own machine-specific tail)
