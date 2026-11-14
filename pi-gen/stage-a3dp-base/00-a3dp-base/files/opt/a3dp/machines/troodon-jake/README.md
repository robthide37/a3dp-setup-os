# Machine profile: troodon-jake (a3dpTR300)

Jake's Formbot Troodon 300 (100.109.20.107, node of **his** tailnet) — first
migration target for the machine-overlay pattern onto the a3dp-image. Scoped by
t_c63f7fb7 (SSH inventory), config modularized by t_5351bc6a (Rob-approved §7
layout, live-verified), overlay built by t_1a6643fd.

## Hardware identity (SSH-verified 2026-11-13/14)

| Part | Identity | Config surface |
|---|---|---|
| Main MCU | Duet 2-class clone, ATSAM4E8E, Klipper-flashed | `[mcu]` serial `/dev/serial/by-id/usb-Klipper_sam4e8e_A3dpmain-if00` (lsusb 1d50:614e). **No RRF conversion, no board swap.** |
| head0 | CAN toolhead, STM32G0B1-class (A3DP family) | `[mcu head0]` `canbus_uuid=d175dd9d6886`, gs_usb (1d50:606f), katapult-capable (`flash/mcus.ini`) |
| host MCU | Pi host-mcu | `[mcu rpi]` `/tmp/klipper_host_mcu` (image bakes `klipper-mcu.service`) |
| Drivers | TMC2660 ×5 (Duex onboard SPI) + SX1509 Duex expander | `hardware/kinematics-zduex.cfg` |
| X/Y | A3DP Leadshine closed-loop servos, **pulse-mode step/dir off the main MCU** — NO FTDI/RS-485 on this machine | `hardware/kinematics-xyservos.cfg` — ported verbatim (`microsteps 1`, `full_steps_per_rotation 4000`, `step_pulse_duration 2.5µs`, `rotation_distance 40`) |
| Probe | Euclid — pure gcode_macros (`EuclidProbe`), no external klippy module | `calibration/euclid.cfg` moved intact, `hardware/probe.cfg` |
| Host | Pi 4B 4GB, Debian 12 | image is PiOS Lite trixie — fine on Pi 4 |
| Panel | BTT-HDMI5 via KlipperScreen (active service) + PanelDue on ttyAMA0 | see decisions |
| Camera | Microdia 0c45:6369, portrait 480×640@15 (1080p-capable) | profile `crowsnest.conf` + `restart.list: crowsnest` |

## Provenance & deviations

`config/` = the **verified post-modular machine tree** (t_5351bc6a handoff
tarball `troodon-final-modular-config.tar.gz`, machine HEAD `96b60cc`,
re-pulled 2026-11-14 and byte-compared) minus intentional exclusions, with
three dry-parse-forced deviations. Everything else is byte-identical.

Excluded (documented, not silent):

| Excluded | Why |
|---|---|
| `moonraker-obico.cfg`, `moonraker-obico-update.cfg` | **machine-local secrets** (Obico auth token). The `[update_manager moonraker-obico]` entry is inlined in the profile `moonraker.conf`; the token is restored from the pre-migration backup at migration (runbook §5). Never bake a per-unit credential. |
| `webui_macros.cfg`, `webui_macros_idex.cfg`, `Jake_macros.cfg` | retired by the t_5351bc6a split; no live includes reference them |
| `.moonraker.conf.bkp`, `moonraker.conf.backup`, `euclid.cfgbak`, `crowsnest.conf.2026-07-22-2019`, `config_backups/` | backups, not config |
| `nozzle.cfg` | not included by the live printer.cfg |
| `macros/a3dp_ir` | stale near-duplicate of `macros/flash`; referenced by nothing |
| `flash/mcu_flasher.cfg` | dangling symlink on the machine itself (target dir absent); live flasher macro is `macros/mcu_flasher.cfg` |
| `components/screen/paneldue.cfg` | unreferenced (PanelDue is a moonraker section, kept in `moonraker.conf`) |

Kept despite looking stale: `macros/flash` (moonraker `[mcu_flasher]` kconfig
sections — **live**, included by `moonraker.conf`) and `flash/` (UKAM script +
kconfig snapshots, verbatim incl. `.old`). Note: the `[mcu_flasher a3dpmain]`
kconfig in `macros/flash` says STM32F446 while the main MCU is ATSAM4E8E —
stale inherited template; flagged, NOT fixed (zero value edits). MCU flashing
goes through the image's `a3dp-mcu-flashd`, not this table.

Dry-parse-forced deviations (each verified against **kalico 84b3e373** — the
image's klippy pin — 2026-11-14, probe: serials/uuid swapped, exit 124 with
only the serial-retry loop, zero config errors):

1. **SAVE_CONFIG `[input_shaper]` block not ported.** Jake's klipper is
   **DangerKlipper** (v0.12.0-568-g23a5b65a, not vanilla) and his shapers are
   Danger-only `smooth_*` types (`smooth_zvd_ei`/`smooth_mzv`). Kalico has no
   smoother support (checked current `main`, not just the pin) — verbatim is
   impossible. The profile boots unshaped; the migration regenerates the
   shaper via ShakeTune on the machine (runbook step 7). All other SAVE_CONFIG
   calibration (bed mesh 15×15, bed/extruder PID, `stepper_z position_endstop
   12.5`, probe `z_offset 5.480`) is Kalico-valid and carried unchanged.
2. **`keep_raw_csv` removed from `[shaketune]`** — option dropped in
   ShakeTune v6 (schema change); remaining options are v6-valid.
3. **`enabled_extruders` commented in `hardware/shaper.cfg`** — removed
   upstream, rejected by Kalico; was already inert on Danger v0.12.

`moonraker.conf` = fleet canonical conf (v0.1.3 line) + three machine
additions, documented in the file header: `[octoprint_compat]`,
`[paneldue]`, `[update_manager moonraker-obico]`.

## Decisions settled (t_1a6643fd)

- **PanelDue: KEEP.** Physical panel on ttyAMA0, zero cost to keep, dropping
  it removes Jake's hardware with no offsetting gain. `[paneldue]` section
  verbatim in `moonraker.conf`; its confirmed macros (`LOAD_FILAMENT`,
  `UNLOAD_FILAMENT`, `LIGHTS_ON`) all exist in the shipped macro files.
- **Obico: STAYS** (Rob's Track A redirect 2026-11-13 — it is the comparison
  baseline for the local-detection-model trial; no frames leave the machine).
  The moonraker-obico *plugin* is not an image component — installed at
  migration time (git clone + venv + `moonraker-obico.service`), token
  restored from backup, then re-linked. The profile only ships the
  `[update_manager]` entry + `moonraker_obico_macros.cfg`.
- **KlipperScreen: migration-time install** (like obico). Jake's unit has the
  BTT-HDMI5; the image doesn't ship KlipperScreen (Qt/PySide weight for a
  display only this machine has). Runbook step, not a first-boot dependency.
- **Update channel: the image IS the update.** No in-place DangerKlipper →
  Kalico stack upgrade before the flash (double churn on a production
  machine, two restart windows instead of one). The flash carries klippy
  Kalico 84b3e373 + Moonraker current; afterwards updates ride the image's
  moonraker update-manager like every fleet unit.
- **Deterministic IDs: kept literal, no tokens.** Single-machine profile:
  `[mcu]` keeps the exact by-id `usb-Klipper_sam4e8e_A3dpmain-if00` (USB
  serial string `A3dpmain` — mcu-flashd's deterministic labeling preserves it
  through the post-flash MCU reflash) and head0 keeps its hardware CAN UUID
  (stable across reflashes). Fail-safe is the image default: if the main MCU
  doesn't enumerate under that exact name, klippy stays down loudly. A future
  multi-Troodon profile should tokenize like meter-idex.
- **First-boot activation:** hostname map `troodon*|a3dptr300*` →
  `troodon-jake` in `a3dp-firstboot` (or `A3DP_MACHINE=troodon-jake` in
  `a3dp-machine.conf` for raw-dd flashes). No token-fill block (see above);
  `restart.list` restarts crowsnest for the profile camera conf;
  `disable.list` empty per printer-profile contract.

## Version-gap path (DangerKlipper 2024-11 → fleet Kalico)

- klippy: Danger v0.12.0-568 → Kalico 84b3e373 (image pin). Config verified
  compatible by dry-parse with the three deviations above.
- Moonraker v0.9.3-101 → image Moonraker. `moonraker.conf` is the canonical
  fleet conf — no gap expected; obico plugin installs against image Moonraker.
- **MCU firmware: reflash required.** Both MCUs run Danger-era firmware;
  protocol will not match image klippy → klippy cannot connect until both are
  reflashed to Kalico-built firmware (image ships the cross toolchain +
  `a3dp-mcu-flashd`). Main sam4e8e over USB, head0 G0B1 over CAN via its
  katapult bootloader. Nothing flashes without Rob's explicit go (runbook).
- ShakeTune: v4.1.0 (klippain, with klipper-file overwrites) → image-baked
  v6.0.0 module-only. Jake's `[shaketune]` section options verified against
  v6.
- timelapse: image bakes moonraker-timelapse; the profile's `timelapse.cfg`
  symlink target (`/home/pi/moonraker-timelapse/klipper_macro/timelapse.cfg`)
  exists in the image.
