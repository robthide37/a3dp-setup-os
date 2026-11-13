#!/usr/bin/env python3
"""Dual-source print-monitor alert relay (Hermes cron script, no_agent watchdog).

Reads the unread alert queues from BOTH monitors and prints them for the
bot-chat delivery:
  - a3dpmeter (Pi 5)  GLM monitor  — primary alerter (bad x2 consecutive)
  - OP5 NPU detector               — confirm-or-N-consecutive alerter (x3)

Double-paging guard: OP5 alerts are suppressed (archived, marked confirmed)
while the Pi 5 has an open incident or alerted within the last 30 min — the
GLM alert already paged; the NPU verdict then just confirms it. OP5 alerts
that arrive with no GLM incident open are the N-consecutive independent
signal and ARE delivered.

Empty output (no alerts) sends nothing. Lives at
/opt/data/profiles/printer/scripts/a3dpmeter_alerts.py; canonical copy in the
printer-hardware repo under monitor/alerts_relay.py.
"""
import json
import subprocess
import sys
import time

KEY = "/opt/data/home/.ssh/id_ed25519_hermes"
SSH_BASE = ["ssh", "-i", KEY, "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]

PI = ("pi@192.168.50.123", "/home/pi/print-monitor")
OP5 = ("orangepi@192.168.50.20", "/home/orangepi/print-op5")
REALERT_WINDOW_S = 30 * 60


def run(cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def read_unread(host, base):
    unread = f"{base}/alerts/unread.jsonl"
    r = run(SSH_BASE + [host, f"if [ -s {unread} ]; then cat {unread}; fi"])
    if r.returncode != 0:
        return None, f"ssh failed (rc={r.returncode}): {r.stderr.strip()[:150]}"
    return [l for l in r.stdout.splitlines() if l.strip()], None


def read_pi_incident_open():
    """True while the Pi 5 GLM monitor has an open incident or alerted recently."""
    r = run(SSH_BASE + [PI[0], f"cat {PI[1]}/state.json 2>/dev/null"])
    if r.returncode != 0 or not r.stdout.strip():
        return False  # can't tell -> don't suppress
    try:
        st = json.loads(r.stdout)
    except json.JSONDecodeError:
        return False
    if st.get("incident_open"):
        return True
    last = st.get("last_alert_ts") or 0
    try:
        return (time.time() - float(last)) < REALERT_WINDOW_S
    except (TypeError, ValueError):
        return False


def archive(host, base):
    unread = f"{base}/alerts/unread.jsonl"
    ts = int(time.time())
    run(SSH_BASE + [host,
        f"mkdir -p {base}/alerts/processed && "
        f"mv {unread} {base}/alerts/processed/unread-{ts}.jsonl"])


def format_alert(a, label):
    out = [f"- `{a.get('ts', '?')}` **{a.get('kind')}** ({label}): {a.get('message')}"]
    d = a.get("detail") or {}
    v = d.get("verdict") or {}
    if v:
        out.append(f"  - verdict: state={v.get('state')} conf={v.get('confidence')} "
                   f"issues={v.get('issues')} notes={v.get('notes')}")
    if d.get("frame_url"):
        out.append(f"  - frame: {d['frame_url']} (open to see the failure)")
    elif d.get("frame"):
        out.append(f"  - frame: {d['frame']} (on {label})")
    if d.get("resume_instruction"):
        out.append(f"  - {d['resume_instruction']}")
    if d.get("pause_result"):
        out.append(f"  - pause: {d['pause_result']} ({d.get('pause_detail')})")
    return out


def main():
    op5_glm_open = read_pi_incident_open()

    delivered = []
    suppressed = []

    # Pi 5 GLM alerts — always delivered (primary alerter)
    pi_lines, err = read_unread(*PI)
    if err:
        print(f"a3dp alert relay: pi5 {err}")
    for l in pi_lines or []:
        try:
            delivered.append((json.loads(l), "a3dpmeter/GLM"))
        except json.JSONDecodeError:
            delivered.append(({"ts": "?", "kind": "raw", "message": l},
                              "a3dpmeter/GLM"))

    # OP5 NPU alerts — delivered only when GLM has no open incident
    op_lines, err = read_unread(*OP5)
    if err:
        print(f"a3dp alert relay: op5 {err}")
    for l in op_lines or []:
        try:
            a = json.loads(l)
        except json.JSONDecodeError:
            a = {"ts": "?", "kind": "raw", "message": l}
        if op5_glm_open:
            a["_confirmed_by_glm"] = True
            suppressed.append(a)
        else:
            delivered.append((a, "op5/NPU"))

    if delivered:
        out = ["🖨️ **Print monitor alert — Rift**"]
        for a, label in delivered:
            out.extend(format_alert(a, label))
        if suppressed:
            out.append(f"  - ({len(suppressed)} OP5 NPU alert(s) confirmed-by-GLM, "
                       f"already alerting — archived without paging)")
        print("\n".join(out))

    if delivered:
        archive(*PI)
    if delivered or suppressed:
        archive(*OP5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
