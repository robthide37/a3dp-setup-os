# a3dp-print-monitor — always-on print monitoring (a3dpmeter → Rift camera → GLM)

Production deployment of the print-monitoring R&D (`monitor-rnd/`, KB
`projects/meter-kit/print-monitoring-rnd.md`). **Read-only against the printer
host** — HTTP GETs only (Moonraker `print_stats`, webcam snapshot). No service
touches, no config edits on the Rift.

## Auto-PAUSE: the confirm listener (t_5d43f750, 2026-11-06)

The monitor's **first WRITE path to a printing machine**. Rob, 2026-11-06:
"have it pause the print if it sees a failure for now as well and then alert
me so I can check if it's actually failed."

```
OP5 NPU detector ──(any detection above threshold; same frame POST)──▶
  Pi 5 confirm listener :8765 (token-gated)
    1. saves the frame → pause-frames/ (served at /frame/<name>)
    2. GLM re-judges the SAME frame (strict confirm prompt, conf ≥ 0.6)
    3. confirm → POST /printer/gcode/script {"script":"PAUSE"} → verify
       pause_resume.is_paused + print_stats.state=paused (10 s poll)
    4. rich alert (auto_paused): frame URL + BOTH judges' verdicts +
       confidences + timestamp + "RESUME if false alarm, CANCEL_PRINT if real"
    5. GLM unreachable/disagrees → alert WITHOUT pausing (manual check)
    6. not printing → short-circuit, no GLM call, no alert
```

### Trust boundary (non-negotiable)

- **PAUSE only on GLM confirmation.** Never pause on the NPU alone — its
  real-world false-positive rate on live frames is unmeasured, and an
  undeserved pause on a healthy print erodes trust in the whole system.
  A missed pause (spaghetti keeps printing) costs filament; a false pause
  costs trust.
- **Degraded mode never gets write privileges**: GLM unreachable → alert
  flagged `*_manual_check`, no pause, regardless of what the NPU says.
- **No pause unless `print_stats.state == printing`** (checked immediately
  before the GLM call; the Rift's PAUSE macro is also idempotent).
- **Auth**: shared bearer token (a3dpmeter `/etc/a3dp-print-monitor/confirm.token`,
  0640 root:pi; OP5 copy 0600 in its own dir) + source-IP allowlist (OP5 +
  localhost). Listener binds :8765 LAN-only; `/frame/<name>` GETs are
  unauthenticated but names carry an 8-hex random suffix.
- **Resume logging**: on pause the listener writes `auto_pause.json`; the 60 s
  monitor watches it — a resume back to `printing` is appended to
  `logs/resumes.jsonl` as calibration data for threshold tuning (expected:
  false-alarm resumes), never paged.
- The Rift's PAUSE macro (webui_macros.cfg, verified 2026-11-05) is a proper
  park macro: retract 1 mm, z+2 mm, park at bed corner, idempotent; RESUME
  restores the saved state — a false-alarm resume costs one retract cycle.

### Verify pause latency budget

Detection (≤60 s NPU cadence) → confirm request (~2 s) → GLM confirm
(~3 s warm) → PAUSE + verify (~2 s). Worst case ≈ 70 s from failure frame
to paused toolhead.

## Architecture

```
Rift (192.168.50.219)                 a3dpmeter (Pi 5, 192.168.50.123)         .232
  nginx :80  /webcam/?action=snapshot ─┐
  Moonraker :7125  print_stats (GET) ──┤─ systemd timer (60 s) → a3dp_print_monitor.py
                                       │    1. only evaluate while print_stats.state=printing
                                       │    2. full-res snapshot → rotate 180° (flip comp.)
                                       │    3. GLM verdict via llama-swap :8080 (brain-glm)
                                       │    4. frames + verdicts archived (7-day retention)
                                       │    5. state!=ok xN / VLM-down → alert file
                                       └─ Hermes cron (5 m, no_agent watchdog): ssh read
                                          unread alerts → deliver bot-chat → archive
```

- **brain-glm is an alias of `glm53-qad2`** — same upstream container as the
  loaded `a3dp-brain` lane. Monitoring adds ~2.7k image tokens/min to the
  always-resident brain; **no llama-swap lane swap, no eviction risk**.
- Warm call ~2.5–3 s @ full-res (2691 image tokens). First call after GPU
  contention queues — that is queueing, not inference.
- The camera ships frames **180°-inverted** (Rift webcam config
  flip_horizontal+flip_vertical). Inverted frames produced false
  `no_print_visible` verdicts (2/2) — `preprocess_rotate_180 = true` rotates
  monitor-side. When the flip is fixed on the Rift (Rob write op), set it false.

## State machine / alerts

- Verdict `ok` → reset counters. `spaghetti|detached|warping|no_print_visible`
  × `alert_consecutive` (2) → one alert per incident, re-alert every
  `realert_minutes` (30) while it persists. `unclear` needs 3 consecutive
  (R&D: 1-in-3 spurious flags on this scene).
- VLM unreachable × `vlm_fail_threshold` (10) → **degraded mode**: frame-diff
  liveness baseline only (naive frame-diff cannot detect failures with a
  moving toolhead in frame — it proves the stream, nothing more). Persistent
  degradation (`degraded_alert_min` = 10) queues an alert. Auto-recovers on
  the next successful VLM call.
- Alerts land in `/home/pi/print-monitor/alerts/unread.jsonl`; the Hermes cron
  job `a3dpmeter-print-monitor-alerts` (job e84a85a50b0d, every 5 m,
  `deliver: bot-chat`) formats, delivers, then archives to `alerts/processed/`.

## Files

| File | Where |
|---|---|
| `a3dp_print_monitor.py` | on a3dpmeter: `/home/pi/monitor/` (venv reuses R&D `~/monitor-rnd/venv`) |
| `confirm_listener.py` | on a3dpmeter: `/home/pi/monitor/` — `a3dp-confirm-listener.service`, :8765 |
| `config.ini.example` | deployed as `/home/pi/monitor/config.ini` (0600 pi) |
| `confirm-listener.ini.example` | deployed as `/home/pi/monitor/confirm-listener.ini` (0600 pi) |
| confirm token | a3dpmeter `/etc/a3dp-print-monitor/confirm.token` (0640 root:pi, never commit); OP5 copy at `/home/orangepi/print-op5/confirm.token` |
| `a3dp-print-monitor.{service,timer}` | `/etc/systemd/system/` on a3dpmeter |
| `install.sh` | run ON a3dpmeter with sudo (idempotent) |
| `alerts_relay.py` | live copy: `/opt/data/profiles/printer/scripts/a3dpmeter_alerts.py` (cron `script:`) |
| llama-swap API key | `/etc/a3dp-print-monitor/llamaswap.key` (0640 root:pi, never committed) |

## Verified live 2026-11-05/06 (t_5d43f750)

- Confirm listener: 401 without token; valid token + real 486 KB frame →
  `not_printing` short-circuit (no GLM call); repeat → 429 cooldown; frame
  archived + served via `GET /frame/<name>` (200, full bytes); path
  traversal → 404. Unit tests `tests/test_confirm_monitor.py` (18/18) cover
  the pause/disagree/unreachable/dry-run/pause-failed decision tree.
- GLM confirm leg: real frame on the brain-glm lane, 3.2 s, strict JSON back,
  correct skeptical verdict (confirm=false on a healthy frame → no pause).
- Write path: `POST /printer/gcode/script {"script":"M117 ..."}` → 200 ok,
  message read back via `display_status` (zero-motion probe; Rift
  state=complete so a live PAUSE+verify could not be exercised yet — first
  real auto-pause on a live print is the final validation; the pause+poll
  code path is covered by tests).
- OP5 → Pi 5 confirm request: real frame + token over HTTP →
  `{"status": "not_printing", ...}` (full client path verified).
- Relay: planted `auto_paused` alert renders frame URL + both verdicts +
  resume instruction + pause result; archived; silent when empty.
- Bug fixed from live testing: `/etc/a3dp-print-monitor` dir must be 750
  root:pi (was root:root — listener couldn't read the token), and the confirm
  prompt is injected via `.replace()` not `.format()` (literal JSON braces).

## Install / upgrade

```bash
# from the repo, on the AI VM:
scp monitor/{a3dp_print_monitor.py,confirm_listener.py,config.ini.example,confirm-listener.ini.example,a3dp-print-monitor.service,a3dp-print-monitor.timer,a3dp-confirm-listener.service,install.sh} pi@192.168.50.123:monitor-stage/
ssh pi@192.168.50.123 'sudo bash ~/monitor-stage/install.sh'
# config updates: sudo install -o pi -g pi -m 600 ~/monitor-stage/config.ini.example ~/monitor/config.ini
```

## Runbook (a3dpmeter)

```bash
# status
systemctl list-timers a3dp-print-monitor.timer
systemctl status a3dp-print-monitor.service
tail -20 ~/print-monitor/logs/monitor.log          # frame + verdict log
tail -5  ~/print-monitor/logs/verdicts.jsonl       # machine-readable verdicts
cat ~/print-monitor/state.json                     # counters / degraded flag
ls ~/print-monitor/alerts/                         # unread.jsonl = undelivered alerts

# force one cycle
sudo systemctl start a3dp-print-monitor.service
journalctl -u a3dp-print-monitor -n 20

# manual relay test (watchdog: empty output = nothing delivered)
python3 /opt/data/profiles/printer/scripts/a3dpmeter_alerts.py

# confirm listener (auto-PAUSE path)
systemctl status a3dp-confirm-listener.service
curl -s http://127.0.0.1:8765/health
tail -20 ~/print-monitor/logs/confirm.log
ls ~/print-monitor/pause-frames/            # frames from confirmed alarms
cat ~/print-monitor/auto_pause.json         # present while an auto-pause is active
tail ~/print-monitor/logs/resumes.jsonl     # false-alarm resume calibration data
```

## Re-pointing to the Big Meter (endgame)

One config change on a3dpmeter — everything else is identical:

```ini
[camera]
snapshot_url = http://<meter-host>/webcam/?action=snapshot   # check nginx :80 proxy first
preprocess_rotate_180 = <only if that cam also ships flipped>
[printer]
moonraker_url = http://<meter-host>:7125
```

If the meter's ustreamer is localhost-only, see the SSH-tunnel fallback in the
KB R&D doc (needs Rob to install a3dpmeter's key on the meter host).

## Known gaps / follow-ups

- `no_print_visible` is treated as a bad state; with rotation the false-flag
  rate on this scene is unmeasured over a long run — the verdicts.jsonl archive
  is the dataset for tuning thresholds (YOLO/GLM disagreement mining later).
- Frames: ~350 MB/day while printing, 7-day retention pruned per cycle.
- If the Rift's `state=printing` but GLM repeatedly says `no_print_visible`
  after rotation, suspect camera aim, not the model — verify a frame by eye
  (monitor skill rule) before changing anything.
