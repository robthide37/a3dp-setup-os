#!/usr/bin/env python3
"""A3DP print monitor — oneshot cycle, runs on a3dpmeter (Pi 5) under systemd timer.

Read-only against the printer host: HTTP GETs only (Moonraker print_stats,
webcam snapshot). Never writes to the printer. GLM verdict via the .232
llama-swap lane; frame-diff baseline when the VLM lane is unreachable.

Cycle:
  1. Moonraker GET print_stats (single-object query — comma-combined returns
     {} on these hosts). Not printing -> reset counters, exit.
  2. Printing -> full-res snapshot -> archive frame -> GLM verdict.
  3. Verdict state != ok for N consecutive cycles -> alert (once per incident,
     re-alert every realert_minutes while it persists).
  4. VLM unreachable for K consecutive cycles -> degraded mode: frame-diff
     baseline (liveness only — naive frame-diff cannot detect failures with a
     moving toolhead in frame); alert if degraded persists. Auto-recovers.

Usage: a3dp_print_monitor.py [--config PATH] [--once]
"""
import argparse
import configparser
import json
import logging
import logging.handlers
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = "/home/pi/monitor/config.ini"

PROMPT = (
    "You are a print monitor. Look at this webcam frame from a 3D printer "
    "mid-print. Answer STRICT JSON: {\"print_visible\": bool, \"state\": "
    "\"ok|spaghetti|detached|warping|unclear|no_print_visible\", "
    "\"confidence\": 0.0-1.0, \"issues\": [..], \"notes\": \"one sentence\"}. "
    "If the print bed or printed object is not visible, say so via state "
    "no_print_visible."
)

# states that count as a print failure (state!=ok in the alert sense)
BAD_STATES = {"spaghetti", "detached", "warping", "no_print_visible"}


def log_event(log, level, msg, **kw):
    if kw:
        log.log(level, "%s %s", msg, json.dumps(kw, default=str))
    else:
        log.log(level, msg)


def http_json(url, timeout, headers=None, data=None, method="GET"):
    req = urllib.request.Request(url, headers=headers or {}, data=data, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def moonraker_print_state(base_url, timeout=10):
    """Single-object Moonraker query. Returns (state, job_name) or raises."""
    res = http_json(f"{base_url}/printer/objects/query?print_stats", timeout)
    ps = res["result"]["status"]["print_stats"]
    return ps.get("state", "unknown"), ps.get("filename", "")


def fetch_snapshot(url, out_path, timeout=30):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if not data or len(data) < 1000:
        raise RuntimeError(f"snapshot too small ({len(data)} B)")
    Path(out_path).write_bytes(data)
    return len(data)


def vlm_check(endpoint, model, api_key, frame_path, timeout):
    """Send a FULL-RES frame to the fleet VLM. Returns (verdict_dict, latency_s, usage)."""
    import base64
    b64 = base64.b64encode(Path(frame_path).read_bytes()).decode()
    payload = {
        "model": model,
        "max_tokens": 3000,
        "temperature": 0.1,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
    }
    t0 = time.perf_counter()
    resp = http_json(
        endpoint, timeout=timeout,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        data=json.dumps(payload).encode(), method="POST")
    dt = time.perf_counter() - t0
    msg = resp["choices"][0]["message"]
    content = msg.get("content") or msg.get("reasoning_content") or ""
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise ValueError(f"no JSON in VLM reply: {content[:200]!r}")
    return json.loads(m.group(0)), round(dt, 2), resp.get("usage")


def frame_diff_stat(cur_path, prev_path):
    """Zero-model liveness baseline. Returns fraction of pixels changed >8/255."""
    try:
        import cv2
        import numpy as np
        a = cv2.imread(str(prev_path), cv2.IMREAD_GRAYSCALE)
        b = cv2.imread(str(cur_path), cv2.IMREAD_GRAYSCALE)
        if a is None or b is None or a.shape != b.shape:
            return None
        diff = cv2.absdiff(a, b)
        return round(float((diff > 8).mean()), 4)
    except Exception:
        return None


def prune_frames(frame_dir, retention_days):
    cutoff = time.time() - retention_days * 86400
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    root = Path(frame_dir)
    if not root.is_dir():
        return
    for d in root.iterdir():
        if d.is_dir() and d.name != day:
            try:
                mtime = d.stat().st_mtime
                if mtime < cutoff:
                    for f in d.iterdir():
                        f.unlink(missing_ok=True)
                    d.rmdir()
                    log_event(log, logging.INFO, "pruned frame day dir", dir=d.name)
            except OSError:
                pass


def write_alert(alerts_file, kind, message, detail):
    """Append one alert line to the unread queue (relay consumes + archives)."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "a3dpmeter-print-monitor",
        "kind": kind,
        "message": message,
        "detail": detail,
    }
    p = Path(alerts_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    log_event(log, logging.WARNING, "ALERT queued", kind=kind, message=message)
    return entry


def load_state(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def save_state(path, st):
    Path(path).write_text(json.dumps(st, indent=1, sort_keys=True))


def main():
    global log
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    args = ap.parse_args()

    # non-blocking instance lock: a cycle still running (VLM queue) => skip.
    # /run/a3dp-print-monitor exists only under systemd (RuntimeDirectory);
    # manual runs fall back to the state dir.
    import fcntl
    lock_fh = None
    for lock_path in ("/run/a3dp-print-monitor/monitor.lock",
                      "/home/pi/print-monitor/monitor.lock"):
        try:
            lock_fh = open(lock_path, "w")
            break
        except OSError:
            continue
    if lock_fh is not None:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another cycle is running; skipping", flush=True)
            return 0

    cfg = configparser.ConfigParser(inline_comment_prefixes=("#",))
    cfg.read(args.config)
    cam_url = cfg["camera"]["snapshot_url"]
    rotate_180 = cfg.getboolean("camera", "preprocess_rotate_180", fallback=False)
    mr_url = cfg["printer"]["moonraker_url"]
    evaluate_states = {s.strip() for s in cfg.get(
        "printer", "evaluate_states", fallback="printing").split(",")}
    endpoint = cfg["vlm"]["endpoint"]
    model = cfg["vlm"]["model"]
    key_file = cfg["vlm"]["api_key_file"]
    vlm_timeout = cfg.getint("vlm", "timeout_s", fallback=120)
    alert_consecutive = cfg.getint("monitor", "alert_consecutive", fallback=2)
    unclear_consecutive = cfg.getint("monitor", "unclear_consecutive", fallback=3)
    fail_threshold = cfg.getint("monitor", "vlm_fail_threshold", fallback=10)
    degraded_alert_min = cfg.getint("monitor", "degraded_alert_min", fallback=10)
    realert_min = cfg.getint("monitor", "realert_minutes", fallback=30)
    frame_dir = Path(cfg["monitor"]["frame_dir"])
    state_file = Path(cfg["monitor"]["state_file"])
    alerts_file = Path(cfg["monitor"]["alerts_file"])
    retention_days = cfg.getint("monitor", "frame_retention_days", fallback=7)

    base = frame_dir.parent
    frame_dir.mkdir(parents=True, exist_ok=True)
    (base / "logs").mkdir(parents=True, exist_ok=True)

    log = logging.getLogger("print-monitor")
    if not log.handlers:
        rh = logging.handlers.RotatingFileHandler(
            base / "logs" / "monitor.log", maxBytes=5_000_000, backupCount=3)
        rh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(rh)
        log.setLevel(logging.INFO)

    st = load_state(state_file)
    now = time.time()
    day_dir = frame_dir / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_dir.mkdir(exist_ok=True)

    # 1. printer state (read-only GET)
    try:
        pstate, job = moonraker_print_state(mr_url)
    except Exception as e:
        log_event(log, logging.WARNING, "moonraker unreachable", error=str(e)[:200])
        st["last_moonraker_error"] = str(e)[:200]
        save_state(state_file, st)
        return 0
    st["last_moonraker_error"] = None

    # Auto-pause follow-up: the confirm listener leaves auto_pause.json when
    # it PAUSEs the print. Track the outcome here — a resume back to printing
    # is logged as calibration data (false-alarm resume candidate), not paged
    # (the pause alert itself already went out).
    marker_file = base / "auto_pause.json"
    marker = None
    try:
        marker = json.loads(marker_file.read_text()) if marker_file.exists() else None
    except Exception:
        marker = None
    if marker:
        if pstate == "paused":
            if not st.get("auto_pause_active"):
                st["auto_pause_active"] = True
                log_event(log, logging.INFO,
                          "auto-pause active (monitor paused the print)",
                          job=marker.get("job"), frame=marker.get("frame"))
        elif pstate == "printing":
            with (base / "logs" / "resumes.jsonl").open("a") as f:
                f.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "event": "resumed_after_auto_pause",
                    "marker": marker}) + "\n")
            log_event(log, logging.INFO,
                      "auto-pause resumed by user — logged as calibration "
                      "data (false-alarm resume candidate)")
            marker_file.unlink(missing_ok=True)
            st["auto_pause_active"] = False
        else:
            log_event(log, logging.INFO,
                      "auto-pause marker cleared: print ended",
                      printer_state=pstate)
            marker_file.unlink(missing_ok=True)
            st["auto_pause_active"] = False

    if pstate not in evaluate_states:
        log_event(log, logging.INFO, "idle", printer_state=pstate)
        for k in ("consecutive_bad", "consecutive_unclear", "vlm_fail_count",
                  "degraded", "degraded_since", "degraded_alerted",
                  "incident_open", "last_alert_ts", "prev_frame"):
            st[k] = None if k == "prev_frame" else (False if k in
                ("degraded", "degraded_alerted", "incident_open") else 0)
        st["printer_state"] = pstate
        st["last_cycle_ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_state(state_file, st)
        prune_frames(frame_dir, retention_days)
        return 0

    # 2. snapshot (full-res, never downscaled — see KB R&D false-flag note)
    frame_path = day_dir / (datetime.now(timezone.utc).strftime("%H%M%S") + ".jpg")
    try:
        nbytes = fetch_snapshot(cam_url, frame_path)
    except Exception as e:
        log_event(log, logging.WARNING, "snapshot failed", error=str(e)[:200])
        st["last_cycle_ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_state(state_file, st)
        return 0
    log_event(log, logging.INFO, "frame captured", file=str(frame_path), bytes=nbytes)
    # orientation compensation: the Rift webcam config has flip_horizontal+
    # flip_vertical set (frames ship 180°-inverted); the R&D false-flag mode.
    # Fixing it on the printer is a Rob write op — we compensate here instead.
    if rotate_180:
        try:
            import cv2
            img = cv2.imread(str(frame_path))
            if img is not None:
                cv2.imwrite(str(frame_path), cv2.rotate(img, cv2.ROTATE_180))
        except Exception as e:
            log_event(log, logging.WARNING, "rotate180 failed, sending raw frame",
                      error=str(e)[:150])

    # 3. VLM verdict
    try:
        api_key = Path(key_file).read_text().strip()
        verdict, latency, usage = vlm_check(endpoint, model, api_key, frame_path, vlm_timeout)
        vstate = verdict.get("state", "unclear")
        conf = verdict.get("confidence")
        log_event(log, logging.INFO, "vlm verdict", state=vstate, confidence=conf,
                  latency_s=latency, frame=str(frame_path), usage=usage)
        with (base / "logs" / "verdicts.jsonl").open("a") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "frame": str(frame_path), "mode": "vlm", "latency_s": latency,
                "verdict": verdict, "usage": usage}) + "\n")

        if st.get("degraded"):
            log_event(log, logging.INFO, "vlm lane recovered, exiting degraded mode")
        st.update({"degraded": False, "degraded_since": None,
                   "degraded_alerted": False, "vlm_fail_count": 0})

        if vstate == "ok":
            st["consecutive_bad"] = 0
            st["consecutive_unclear"] = 0
            if st.get("incident_open"):
                log_event(log, logging.INFO, "incident closed (verdict back to ok)")
                st["incident_open"] = False
        elif vstate == "unclear":
            st["consecutive_unclear"] = st.get("consecutive_unclear", 0) + 1
            if (st["consecutive_unclear"] >= unclear_consecutive
                    and not st.get("incident_open")):
                write_alert(alerts_file, "unclear",
                            f"GLM verdict 'unclear' x{st['consecutive_unclear']} on {job}",
                            {"verdict": verdict, "frame": str(frame_path)})
                st["incident_open"] = True
                st["last_alert_ts"] = now
        else:
            st["consecutive_bad"] = st.get("consecutive_bad", 0) + 1
            st["consecutive_unclear"] = 0
            if (st["consecutive_bad"] >= alert_consecutive
                    and not st.get("incident_open")):
                write_alert(alerts_file, vstate,
                            f"GLM verdict '{vstate}' x{st['consecutive_bad']} on {job}",
                            {"verdict": verdict, "frame": str(frame_path)})
                st["incident_open"] = True
                st["last_alert_ts"] = now
            elif st.get("incident_open") and st.get("last_alert_ts") and \
                    now - st["last_alert_ts"] > realert_min * 60:
                write_alert(alerts_file, vstate,
                            f"still failing: GLM verdict '{vstate}' on {job} "
                            f"(re-alert after {realert_min} min)",
                            {"verdict": verdict, "frame": str(frame_path)})
                st["last_alert_ts"] = now
    except Exception as e:
        # 4. degraded path: VLM lane down
        st["vlm_fail_count"] = st.get("vlm_fail_count", 0) + 1
        err = str(e)[:250]
        log_event(log, logging.WARNING, "vlm call failed",
                  fail_count=st["vlm_fail_count"], error=err)
        if st["vlm_fail_count"] >= fail_threshold:
            if not st.get("degraded"):
                st["degraded"] = True
                st["degraded_since"] = now
                st["degraded_alerted"] = False
                log_event(log, logging.WARNING,
                          "degraded mode: VLM lane unreachable, frame-diff baseline only")
            prev = st.get("prev_frame")
            change = frame_diff_stat(frame_path, prev) if prev else None
            with (base / "logs" / "verdicts.jsonl").open("a") as f:
                f.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "frame": str(frame_path), "mode": "degraded_frame_diff",
                    "change_frac": change, "error": err}) + "\n")
            log_event(log, logging.INFO, "degraded frame-diff baseline",
                      change_frac=change)
            if (st.get("degraded") and not st.get("degraded_alerted")
                    and st.get("degraded_since")
                    and now - st["degraded_since"] > degraded_alert_min * 60):
                write_alert(alerts_file, "monitor_degraded",
                            f"print monitor degraded: VLM lane unreachable for "
                            f"{int((now - st['degraded_since']) / 60)} min "
                            f"(frame-diff baseline only)",
                            {"last_error": err})
                st["degraded_alerted"] = True

    st["prev_frame"] = str(frame_path)
    st["printer_state"] = pstate
    st["last_cycle_ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_state(state_file, st)
    prune_frames(frame_dir, retention_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
