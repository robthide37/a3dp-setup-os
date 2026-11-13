# Bed-occupancy AI gate — machine-side layer (Rift 525 / a3dp525)

Task t_1ce4ee85. Split: **printer** owns this layer (gate logic, detection spec,
calibration-macro integration); **coder** owns the companion daemon (see
`GATE-CONTRACT.md`). Sparked by the run-391 incident (probe routine ran without
bed-occupancy verification).

## Deployed live on a3dp525 (2026-11-12)

- `macros/bed-gate.cfg` — new file, included from `printer.cfg` line 3
- `integrations/*.patch` — the exact edits applied to `printer.cfg`,
  `webui_macros.cfg` (PRINT_START preflight + CALIBRATE_Z), `eddyprobe.cfg`
  (PROBE_CALIBRATE + PROBE_SETUP)
- Backups: pi-side `~/printer_data/config/config_backups/bedgate-20261112-165758/`
  (pre-gate originals)

## Design (details in bed-gate.cfg header comments)

- **Fail-open** is the core rule: daemon death / stale state / klippy restart all
  leave the machine printable. Klipper Jinja has no wall clock, so staleness is a
  klippy-side tick counter (`delayed_gcode BED_GATE_TICK`, 60 s; daemon resets it).
- Gates: PRINT_START preflight, BED_MESH_CALIBRATE, QUAD_GANTRY_LEVEL,
  PROBE_CALIBRATE, PROBE_SETUP, CALIBRATE_Z. **Standalone G28 is deliberately NOT
  gated** — rename_existing is structurally impossible for traditional commands and
  `[homing_override]` conflicts with this machine's `[safe_z_home]` (see comments in
  bed-gate.cfg). Homing inside PRINT_START is gated.
- Human override: `BED_GATE_OVERRIDE MINUTES=10` (self-expires, logged) or daemon API.
  Kill switch: `BED_GATE_DISABLE` / `BED_GATE_ENABLE`.

## Verified live (2026-11-12, 12/12 PASS)

| # | Test | Result |
|---|------|--------|
| 1 | `_BED_GATE_CHECK` blocks (HTTP 400) when `bed_clear=False`, stores `last_reason` | PASS |
| 2 | passes when clear | PASS |
| 3 | fail-open when daemon stale (ticks=999, bed_clear=False) | PASS |
| 4 | `BED_GATE_OVERRIDE` activates + logs `override_by=console_2min` | PASS |
| 5 | check passes under override | PASS |
| 6 | `BED_MESH_CALIBRATE` blocks before motion | PASS |
| 7 | `QUAD_GANTRY_LEVEL` blocks before motion | PASS |
| 8 | `PROBE_CALIBRATE` blocks | PASS |
| 9 | `PROBE_SETUP` blocks | PASS |
| 10 | `CALIBRATE_Z` blocks | PASS |
| 11 | `G28 X` homes when clear (homing pass-through intact) | PASS |
| 12 | final state = fail-open defaults | PASS |

## Kalico gotchas hit during deployment (each cost a rollback; documented in-file)

1. Kalico's Jinja env uses **single-brace variables** (`{var}`, not `{{var}}`).
2. `delayed_gcode` has **no `interval`** — repeat via self-rescheduling
   `UPDATE_DELAYED_GCODE` (sets `repeat` when called inside the timer body).
3. `gcode_macro rename_existing` is impossible for traditional commands (G28) — the
   dispatcher reads only the first letter+number token, so `G28_1` re-dispatches as
   `G28` → recursion.
4. `[homing_override]` and `[safe_z_home]` are mutually exclusive.
5. `SET_GCODE_VARIABLE VALUE` strings need **double-wrapped quotes**
   (`VALUE='"text"'`) and **no spaces** (param split + `ast.literal_eval`).
6. `action_*` calls fire at **template-render time** — a render-time raise aborts the
   macro before earlier rendered gcode lines execute. Deferred raises must live in a
   helper macro (here: `_BED_GATE_BLOCK`).

## Daemon side (coder)

Read `GATE-CONTRACT.md` fully before writing code — the fail-open heartbeat contract
and the job_start pause semantics are safety decisions, not preferences.
Detection method + tuning procedure: `DETECTION-SPEC.md`.
