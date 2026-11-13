# GATE-CONTRACT.md — a3dp-bedgate daemon ↔ machine contract (t_1ce4ee85)

**Split:** printer owns this contract + all Klipper-side logic (`bed-gate.cfg`, deployed
live on a3dp525/Rift). **Coder owns the daemon plumbing** per the umbrella ownership
split. Read this whole file before writing daemon code — every behavior here is a
safety decision, not a preference.

## Machine state (already deployed in `macros/bed-gate.cfg`)

The daemon talks ONLY to these, over Moonraker `POST /printer/gcode/script` (LAN-open,
`trusted_clients` covers all RFC1918 — no API key needed on this unit):

```
SET_GCODE_VARIABLE MACRO=BED_GATE VARIABLE=bed_clear            VALUE=True|False
SET_GCODE_VARIABLE MACRO=BED_GATE VARIABLE=ticks_since_update   VALUE=0        # every write!
SET_GCODE_VARIABLE MACRO=BED_GATE VARIABLE=override_active      VALUE=True|False
SET_GCODE_VARIABLE MACRO=BED_GATE VARIABLE=override_ticks_left  VALUE=<minutes>
SET_GCODE_VARIABLE MACRO=BED_GATE VARIABLE=override_by          VALUE={ "'" ~ '"daemon_' ~ <who> ~ '"' ~ "'" }
SET_GCODE_VARIABLE MACRO=_BED_GATE_CHECK VARIABLE=gate_enabled  VALUE=False    # kill switch only
```

**String VALUE gotcha (verified live):** klippy strips ONE layer of outer quotes from
param values before `ast.literal_eval`, so a string literal must be double-wrapped —
rendered form `VALUE='"daemon_robs_phone"'` (outer singles stripped by the parser,
inner doubles make it a string). Values must contain NO spaces (whitespace splits the
param); use underscores. Booleans/ints are bare (`VALUE=True`, `VALUE=0`).

Read state back via `GET /printer/objects/query?gcode_macro=BED_GATE&_BED_GATE_CHECK=...`
(use `print_stats`, `pause_resume` too). Status string: gcode `BED_GATE_STATUS`.

Macros the gate owns: `BED_GATE_OVERRIDE`, `BED_GATE_STATUS`, `BED_GATE_DISABLE/ENABLE`,
`_BED_GATE_CHECK` (called as first line of PRINT_START, G28, BED_MESH_CALIBRATE,
QUAD_GANTRY_LEVEL, PROBE_CALIBRATE, PROBE_SETUP, CALIBRATE_Z).

## Daemon responsibilities ( coder implements; a3dp-flashd pattern)

stdlib-only Python, own systemd unit on the **Rift Pi** (a3dp525), own port + nginx LAN
allowlist (:8083 suggested — :8081 decommissioned, :8082 is mcu-flashd). REST + one
websocket connection to Moonraker (:7125).

### 1. Occupancy ingest
- Detection worker(s) POST verdicts: `POST /api/occupancy`
  `{"occupied": bool, "confidence": 0.0-1.0, "reason": str, "ts": <epoch>, "frame": <path|null>}`
- Detection substrate & tuning: see DETECTION-SPEC.md (printer-owned). Detection can run
  off-box (a3dpmeter lane) — the daemon only requires a reachable HTTP verdict source.
- **Daemon MUST NOT invent occupancy itself.** No detection = no verdict = stale = fail open.
- Every accepted verdict → write `bed_clear` + `ticks_since_update=0` (one gcode/script
  call is fine, two commands in one script string).

### 2. Heartbeat (REQUIRED for fail-open)
- Write `ticks_since_update=0` at least every **120 s**, verdict changed or not. The
  klippy tick counter marks the gate stale after 5 min of silence and the macros then
  FAIL OPEN. No heartbeat = machine prints unguarded; never let a bug suppress it.
- On graceful shutdown (systemd `ExecStop`): set `bed_clear=True`, `ticks_since_update=0`,
  emit a console warning "bed gate offline — passing (fail-open)". On startup: same reset
  before opening the API port.

### 3. Hard gate on Moonraker `job_start`
- Subscribe to Moonraker websocket (`job_start` event). On event:
  1. Query `gcode_macro BED_GATE` + `print_stats` + `pause_resume`.
  2. If `override_active` → log override entry, emit `info` console message, DONE (no pause).
  3. If `bed_clear` False **and** gate fresh (ticks ≤ stale_ticks) →
     `POST /printer/gcode/script {"script": "PAUSE"}` + klippy console `RESPOND TYPE=error` (red in Mainsail console) + `M117` status
     "Bed occupied — job paused. Remove part, then RESUME (logged override)".
     Delay the PAUSE ~2 s and skip it if the job already died (the macro preflight
     usually aborts PRINT_START first — `action_raise_error` kills the job; a stray
     PAUSE on an idle machine just parks, but don't add noise).
  4. **NEVER auto-resume and NEVER auto-cancel.** The print stays paused until a human
     acts. If still paused after 10 min → escalate to repeated `RESPOND TYPE=error` + prompt, repeat hourly.
  5. Human RESUME within the pause = the logged override (watch `pause_resume` /
     `print_stats` transition; write a JSONL override log line + `info` console message).
- Mid-print occupancy marking: once a job is printing, wait until
  `print_stats.print_duration > 120 s` before setting `bed_clear=False` (the part is
  then legitimately on the bed and mid-print G28/mesh/QGL get blocked by the macro's
  mid-print rule). Before 120 s, the pre-start verdict stands.

### 4. Override API (equivalent of BED_GATE_OVERRIDE, for UI)
- `POST /api/override {"minutes": N, "who": str}` → writes the same variables +
  JSONL log. Override always explicit, always logged (`who` is mandatory).
- `GET /api/state` → bed_clear, ticks, override, last verdict, last frame path.

### 5. Warning layer — what actually surfaces in stock Mainsail (verified live)
- **Moonraker announcements CANNOT be used**: this unit runs Moonraker v0.11.0
  (`v0.11.0-4-gb559b9a`), whose announcements component has NO post API — only
  `list/dismiss/update/feed/feeds` (RSS-feed-derived). Verified: `POST
  /server/announcements/post` → 404, no `post_announcement` remote method exists.
- Warning surface (all stock UI, no fork):
  1. klippy console: `RESPOND TYPE=error MSG=...` (renders red in Mainsail console),
     `RESPOND TYPE=command` for info-level.
  2. `M117 <msg>` status line for persistent at-a-glance state.
  3. klippy prompts (`action:prompt_begin` / `action:prompt_button`) for the override
     CTA — Mainsail and KlipperScreen both render prompts.
  4. the daemon's own web UI (:8083) for verdict detail + override button + logs.
- Optional later: Moonraker `[notifier]` for Telegram push (config change, needs
  printer approval).

### 6. Fail-open checklist (verify each in the daemon's own tests)
- [ ] daemon killed -9 → no writes → klippy tick goes stale → macros pass with warning
- [ ] systemd stop → ExecStop writes fail-open state + console warning
- [ ] detection worker dead → daemon marks verdict stale, does NOT write bed_clear=False
- [ ] klippy restart → variables reset to fail-open defaults; daemon re-syncs within 120 s
- [ ] moonraker down → daemon queues nothing destructive; retries; no writes lost silently

## What the daemon must NOT do
- Never write Klipper config files, never restart klippy/moonraker, never write
  registers or run FLASH paths. Gate daemon = verdicts + console warnings + PAUSE + logs.
- Never set `bed_clear=True` on a guess. Only from a fresh detection verdict or a
  human override, or on daemon start/stop (fail-open reset).
