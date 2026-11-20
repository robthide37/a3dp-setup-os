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

## Deployed on a3dp4006mm (.101) — 2026-11-20 (t_39bbe296)

- `macros/bed-gate.cfg` + `[include macros/bed-gate.cfg]` in printer.cfg; hooks
  in `A3dp/webui_macros.cfg` (PRINT_START) and `euclid.cfg`
  (BED_MESH_CALIBRATE, QUAD_GANTRY_LEVEL, PROBE_CALIBRATE, probe_setup,
  HOMETEST, HOME_LVL_MESH, SETUP_RUN_QGL). **CALIBRATE_Z does not exist on
  .101 — nothing to hook.** Standalone G28 remains deliberately ungated
  (structural reasons, see comments in bed-gate.cfg).
- The bed-gate.cfg **builtin wrappers are stripped for .101** (euclid.cfg
  already wraps those names) — deploy via `deploy_bed_gate_101.py`, which
  enforces that.
- Backups: pi-side `config_backups/bedgate-20261120-022204/` (pre-gate
  originals).
- **BED_GATE_OVERRIDE now REQUIRES `WHO=`** (no anonymous override; the popup
  buttons pass `WHO=klipper_prompt`). NOTE for the next Rift update: Rift's
  live copy still has the old console-default signature — decide consciously
  before redeploying there.
- **Single-wire Euclid conversion (Rob, 2026-11-20): BLOCKED on a physical
  wiring check.** Measured live: X endstop on `head0:PB11` read TRIGGERED at
  rest for plain, `!` AND `^!` claims (with `^` the internal pullup held it
  HIGH) → the pin is UNDRIVEN — the Euclid signal wire is not landing on
  PB11 (or was disturbed during the 2026-11-20 head0 repair session). PA14
  reads consistently open and homing works. **PA14 left in place**; run
  `deploy_bed_gate_101.py --with-pin-change` after the wiring is fixed (it
  auto-tries plain → `!` → `^!` and drops stepper_x homing_speed to 30 for
  the first trigger verification). Also flagged: the machine's [probe] (pin
  head0:PB11) reads triggered-at-rest on a live QUERY_PROBE — probing may be
  broken right now independent of the gate.
- Verified live (2026-11-20, all PASS): 9 sections after restart,
  endstop/probe state-match, BED_GATE_STATUS, fail-open pass on
  occupied+stale (no motion), WHO refusal (override_active stays False),
  occupied verdict → block with last_reason, low-confidence verdict
  rejected, TTL expiry → fail-open restore, kill -9 → auto-restart +
  startup reset, ExecStop → fail-open write + console warning, heartbeat
  ticks=0, X homing G28 X at the dock (PA14 path unchanged), daemon gate
  pause + Mainsail prompt popup + button override (who=klipper_prompt) on a
  dummy job, RESUME/cancel end-to-end.
