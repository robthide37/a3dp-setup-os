#!/usr/bin/env python3
"""Disagreement miner — correlates OP5 NPU verdicts with Pi 5 GLM verdicts.

The training-sample miner from the print-monitoring skill's self-improving
dataset loop: every YOLO-vs-GLM disagreement on a paired frame is a labeled
candidate. Runs on the AI VM as a Hermes cron (no_agent); the canonical
verdict logs stay on the SBCs (never pruned) — this log is derived data and
fully reconstructable from them.

Pairing: both monitors fire minute-aligned (OnCalendar=*:*:00) and name
frames by UTC capture time (frames/YYYY-MM-DD/HHMMSS.jpg). Pairs form when
capture timestamps are within PAIR_WINDOW_S (5 s). Verdict-log `ts` fields
are completion times and are NOT used for pairing.

Disagreement classes (gl = GLM state, np = NPU state):
  yolo_only_flag   GLM ok, NPU flags failure          <- prime spaghetti/stringing sample
  glm_only_flag    GLM flags failure, NPU ok          <- possible GLM false positive or YOLO miss
  class_mismatch   both flag failure, different classes
  glm_unsure       GLM unclear/no_print_visible, NPU ok (also glm unsure + NPU fail -> glm_only_flag)

Output: DISAGREEMENT_FILE (JSONL, deduped by pair key). Frame sha256 for both
sides is computed over SSH per new disagreement (frames stay on their boxes,
7-day retention).
"""
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

KEY = "/opt/data/home/.ssh/id_ed25519_hermes"
SSH_BASE = ["ssh", "-i", KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]

PI = ("pi@192.168.50.123", "/home/pi/print-monitor")
OP5 = ("orangepi@192.168.50.20", "/home/orangepi/print-op5")
TAIL_LINES = 3000  # ~50 h at a 60 s cadence; dedupe makes overlap free

DATA_DIR = Path("/opt/data/projects/printer-hardware/data/dual-run")
DISAGREEMENT_FILE = DATA_DIR / "disagreements.jsonl"
PAIR_WINDOW_S = 5

GLM_BAD = {"spaghetti", "detached", "warping"}


def ssh_read(host, cmd, timeout=90):
    r = subprocess.run(SSH_BASE + [host, cmd], capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"ssh {host} rc={r.returncode}: {r.stderr.strip()[:200]}")
    return r.stdout


def ssh_sha256(host, path, timeout=60):
    out = ssh_read(host, f"sha256sum '{path}'", timeout).strip().split()[0]
    return out


def load_jsonl(text, source):
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            e["_source"] = source
            entries.append(e)
        except json.JSONDecodeError:
            continue
    return entries


def capture_ts(entry):
    """UTC capture timestamp parsed from the frame path (frames/<day>/HHMMSS.jpg)."""
    frame = entry.get("frame", "")
    try:
        day = frame.split("/")[-2]
        hhmmss = Path(frame).stem
        return datetime.strptime(f"{day} {hhmmss}", "%Y-%m-%d %H%M%S").replace(
            tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def classify(gl_state, np_state):
    gl_fail = gl_state in GLM_BAD
    np_fail = np_state in {"spaghetti", "stringing", "zits"}
    if gl_state in {"unclear", "no_print_visible"}:
        return "glm_only_flag" if np_fail else "glm_unsure"
    if gl_fail and np_fail:
        return "agreement" if gl_state == np_state else "class_mismatch"
    if gl_fail and not np_fail:
        return "glm_only_flag"
    if np_fail and not gl_fail:
        return "yolo_only_flag"
    return "agreement"


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    seen = set()
    if DISAGREEMENT_FILE.exists():
        for line in DISAGREEMENT_FILE.read_text().splitlines():
            try:
                seen.add(json.loads(line)["pair_key"])
            except Exception:
                continue

    pi_log = f"tail -n {TAIL_LINES} {PI[1]}/logs/verdicts.jsonl 2>/dev/null"
    op_log = f"tail -n {TAIL_LINES} {OP5[1]}/logs/verdicts.jsonl 2>/dev/null"
    pi_entries = load_jsonl(ssh_read(PI[0], pi_log), "pi5-glm")
    op_entries = load_jsonl(ssh_read(OP5[0], op_log), "op5-npu")

    pi_by_ts = {}
    for e in pi_entries:
        if e.get("mode") != "vlm":
            continue
        t = capture_ts(e)
        if t is not None:
            pi_by_ts.setdefault(round(t), []).append((e, t))

    stats = {"pairs": 0, "disagreements": 0, "agreements": 0, "new": 0,
             "no_pair": 0}
    by_type = {}
    new_rows = []
    for op in op_entries:
        if op.get("mode") != "npu" or op.get("forced"):
            continue
        t = capture_ts(op)
        if t is None:
            continue
        cand = None
        cand_ts = None
        for dt in range(-PAIR_WINDOW_S, PAIR_WINDOW_S + 1):
            for pe, pt in pi_by_ts.get(round(t) + dt, []):
                if cand is None or abs(pt - t) < abs(cand_ts - t):
                    cand, cand_ts = pe, pt
        if cand is None or cand_ts is None:
            stats["no_pair"] += 1
            continue
        stats["pairs"] += 1
        gl_v = cand.get("verdict") or {}
        np_v = op.get("verdict") or {}
        gl_state, np_state = gl_v.get("state", "?"), np_v.get("state", "?")
        dtype = classify(gl_state, np_state)
        by_type[dtype] = by_type.get(dtype, 0) + 1
        if dtype == "agreement":
            stats["agreements"] += 1
            continue
        stats["disagreements"] += 1
        key = f"{int(cand_ts)}:{int(t)}"
        if key in seen:
            continue
        seen.add(key)
        row = {
            "pair_key": key,
            "mined_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "type": dtype,
            "glm": {"host": PI[0], "state": gl_state,
                    "confidence": gl_v.get("confidence"),
                    "issues": gl_v.get("issues"), "notes": gl_v.get("notes"),
                    "frame": cand.get("frame"),
                    "frame_ts": datetime.fromtimestamp(
                        cand_ts, timezone.utc).isoformat(timespec="seconds")},
            "npu": {"host": OP5[0], "state": np_state,
                    "confidence": np_v.get("confidence"),
                    "detections": np_v.get("detections"),
                    "frame": op.get("frame"),
                    "frame_sha256": op.get("frame_sha256"),
                    "frame_ts": datetime.fromtimestamp(t, timezone.utc).isoformat(
                        timespec="seconds")},
        }
        try:
            row["glm"]["frame_sha256"] = ssh_sha256(PI[0], cand["frame"])
        except Exception as e:
            row["glm"]["frame_sha256_error"] = str(e)[:150]
        new_rows.append(row)
        stats["new"] += 1

    if new_rows:
        with DISAGREEMENT_FILE.open("a") as f:
            for row in new_rows:
                f.write(json.dumps(row) + "\n")

    print(json.dumps({"stats": stats, "by_type": by_type,
                      "log_total": len(seen),
                      "log_file": str(DISAGREEMENT_FILE)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
