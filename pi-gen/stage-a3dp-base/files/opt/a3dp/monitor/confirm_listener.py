#!/usr/bin/env python3
"""A3DP confirm listener — Pi 5 (a3dpmeter) HTTP endpoint that turns an OP5
NPU alarm into a GLM-confirmed auto-PAUSE. Runs as a systemd service beside
the 60 s monitor timer.

TRUST BOUNDARY — the monitor's first WRITE path to a printing machine:

  1. OP5 NPU detector flags a failure (any detection above threshold) and
     POSTs the SAME frame here.
  2. This listener re-judges that exact frame with GLM (strict confirm
     prompt, higher bar than the passive monitor).
  3. PAUSE is sent ONLY if GLM confirms. Never pause on the NPU alone —
     its real-world false-positive rate on live frames is unmeasured, and
     an undeserved pause on a healthy print erodes trust in the system.
  4. GLM unreachable or disagreeing -> alert WITHOUT pausing, flagged for
     manual check. Degraded mode never gets write privileges.
  5. No pause unless Moonraker reports print_stats.state == printing.
  6. Auth: shared bearer token + source-IP allowlist (OP5 + localhost only).

The Rift's PAUSE macro (webui_macros.cfg, verified 2026-11-05) is idempotent
("Already paused — ignoring PAUSE"), parks the toolhead with retract + z-hop;
RESUME restores the saved state. So a false-alarm resume costs nothing but a
retract/recover cycle — and every resume is logged as calibration data.

Alerts land in the shared unread queue (relay delivers + archives); the
failed frame is served by this listener at /frame/<name> so the alert can
carry a clickable LAN link.
"""
import argparse
import base64
import configparser
import fcntl
import json
import logging
import logging.handlers
import random
import re
import socketserver
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

DEFAULT_CONFIG = "/home/pi/monitor/confirm-listener.ini"

# States GLM must report (>= confirm_min_conf) to authorize a pause.
BAD_STATES = {"spaghetti", "detached", "warping", "no_print_visible"}

CONFIRM_PROMPT = (
    "You are the CONFIRMING judge for an automated print-failure pause "
    "system. A YOLO detector flagged this webcam frame from a 3D printer "
    "mid-print as a failure (detections: {detections}). Examine the frame "
    "carefully. Confirm ONLY if the print is ACTIVELY failing (spaghetti, "
    "object detached from the bed, severe warping/lifting). A healthy print "
    "or ambiguous frame must be answered confirm=false — a wrong auto-pause "
    "on a healthy print is worse than a missed pause. Answer STRICT JSON: "
    "{\"confirm\": bool, \"state\": "
    "\"ok|spaghetti|detached|warping|unclear|no_print_visible\", "
    "\"confidence\": 0.0-1.0, \"reason\": \"one sentence\"}."
)
# NOTE: injected via .replace() — the prompt contains literal JSON braces the
# model must echo, so str.format() would raise KeyError on them.


def log_event(log, level, msg, **kw):
    if kw:
        log.log(level, "%s %s", msg, json.dumps(kw, default=str))
    else:
        log.log(level, msg)


def http_json(url, timeout, headers=None, data=None, method="GET"):
    req = urllib.request.Request(url, headers=headers or {}, data=data, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def moonraker_query(base_url, obj, timeout=10):
    """Single-object Moonraker query (comma-combined returns {} on these hosts)."""
    res = http_json(f"{base_url}/printer/objects/query?{obj}", timeout)
    return res["result"]["status"].get(obj, {})


def glm_confirm(endpoint, model, api_key, frame_path, detections, timeout):
    """Re-judge the alarm frame with GLM. Returns (verdict_dict, latency_s)."""
    b64 = base64.b64encode(Path(frame_path).read_bytes()).decode()
    payload = {
        "model": model,
        "max_tokens": 3000,
        "temperature": 0.1,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text",
                 "text": CONFIRM_PROMPT.replace(
                     "{detections}", json.dumps(detections))},
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
    dt = round(time.perf_counter() - t0, 2)
    msg = resp["choices"][0]["message"]
    content = msg.get("content") or msg.get("reasoning_content") or ""
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise ValueError(f"no JSON in VLM reply: {content[:200]!r}")
    return json.loads(m.group(0)), dt


def decide(glm_verdict, confirm_min_conf):
    """Two-judge decision rule. Returns (action, detail_str).

    action is "pause" (GLM confirms a bad state at/above the confidence
    floor) or "manual" (GLM disagrees — never pause on the NPU alone).
    """
    state = glm_verdict.get("state", "unclear")
    conf = glm_verdict.get("confidence")
    try:
        conf = float(conf) if conf is not None else 0.0
    except (TypeError, ValueError):
        conf = 0.0
    if glm_verdict.get("confirm") is True and state in BAD_STATES \
            and conf >= confirm_min_conf:
        return "pause", f"glm state={state} conf={conf}"
    return "manual", f"glm state={state} conf={conf} (below pause bar)"


def send_pause(moonraker_url, pause_script, verify_s, log):
    """POST the pause script and verify it landed. Returns (ok, detail)."""
    try:
        http_json(f"{moonraker_url}/printer/gcode/script", timeout=15,
                  headers={"Content-Type": "application/json"},
                  data=json.dumps({"script": pause_script}).encode(),
                  method="POST")
    except Exception as e:
        return False, f"pause POST failed: {str(e)[:200]}"
    deadline = time.time() + verify_s
    while time.time() < deadline:
        time.sleep(1.0)
        try:
            pr = moonraker_query(moonraker_url, "pause_resume")
            ps = moonraker_query(moonraker_url, "print_stats")
            if pr.get("is_paused") and ps.get("state") == "paused":
                return True, "verified: pause_resume.is_paused=true, state=paused"
            if ps.get("state") not in ("printing", "paused"):
                return False, f"print left printing during pause (state={ps.get('state')})"
        except Exception as e:
            log_event(log, logging.WARNING, "pause verify poll failed", error=str(e)[:150])
    return False, "pause not verified within deadline"


def write_alert(alerts_file, kind, message, detail, log):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "a3dpmeter-confirm-listener",
        "kind": kind,
        "message": message,
        "detail": detail,
    }
    p = Path(alerts_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    log_event(log, logging.WARNING, "ALERT queued", kind=kind, message=message)


def write_marker(marker_file, payload):
    """Atomically record the auto-pause so the 60 s monitor can log the
    resume (false-alarm resumes are calibration data, not alerts)."""
    p = Path(marker_file)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True))
    tmp.replace(p)


def clear_marker(marker_file):
    Path(marker_file).unlink(missing_ok=True)


class ConfirmState:
    """Process-wide state: serializes confirm processing (GLM calls block)."""

    def __init__(self):
        import threading
        self.sem = threading.Lock()
        self.last_confirm_ts = 0.0


def process_confirm(cfg, st, req, client_ip, log, now=None):
    """Core confirm flow, shared by the HTTP handler and tests.

    cfg: parsed configparser; st: ConfirmState; req: parsed request dict.
    Returns (http_status, response_dict).
    """
    now = now if now is not None else time.time()
    token = Path(cfg["listener"]["token_file"]).read_text().strip()
    if req.get("_token") != token:
        return 401, {"status": "unauthorized"}
    allowed = {s.strip() for s in cfg.get("listener", "allowed_ips",
                                          fallback="127.0.0.1").split(",")}
    if client_ip not in allowed:
        return 403, {"status": "forbidden_source", "source": client_ip}

    cooldown = cfg.getint("listener", "cooldown_s", fallback=300)
    if now - st.last_confirm_ts < cooldown:
        return 429, {"status": "cooldown",
                     "retry_after_s": round(cooldown - (now - st.last_confirm_ts), 1)}

    try:
        frame_bytes = base64.b64decode(req.get("frame_b64", ""))
    except Exception:
        return 400, {"status": "bad_request", "error": "frame_b64 not decodable"}
    if len(frame_bytes) < 1000:
        return 400, {"status": "bad_request", "error": f"frame too small ({len(frame_bytes)} B)"}

    st.last_confirm_ts = now  # accepted request — start the cooldown clock

    frame_dir = Path(cfg["listener"]["pause_frame_dir"])
    frame_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"{random.randrange(16**8):08x}"
    frame_path = frame_dir / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"_{suffix}.jpg")
    frame_path.write_bytes(frame_bytes)
    frame_url = f"{cfg['listener'].get('frame_url_base', '').rstrip('/')}/frame/{frame_path.name}"

    detections = req.get("detections") or []
    npu_verdict = req.get("npu_verdict") or {}
    job = ""
    mr_url = cfg["printer"]["moonraker_url"]
    try:
        ps = moonraker_query(mr_url, "print_stats")
        job = ps.get("filename", "")
        pstate = ps.get("state", "unknown")
    except Exception as e:
        log_event(log, logging.WARNING, "moonraker unreachable", error=str(e)[:200])
        return 502, {"status": "moonraker_unreachable", "error": str(e)[:200]}

    if pstate != "printing":
        log_event(log, logging.INFO, "confirm skipped: not printing",
                  printer_state=pstate, frame=str(frame_path))
        return 200, {"status": "not_printing", "printer_state": pstate,
                     "frame": str(frame_path)}

    # GLM re-judges the SAME frame (the confirming judge; never pause on NPU alone)
    try:
        api_key = Path(cfg["vlm"]["api_key_file"]).read_text().strip()
        glm, glm_latency = glm_confirm(
            cfg["vlm"]["endpoint"], cfg["vlm"]["model"], api_key,
            frame_path, detections, cfg.getint("vlm", "timeout_s", fallback=120))
    except Exception as e:
        # degraded mode: no GLM -> NO write privileges, manual check only
        err = str(e)[:250]
        log_event(log, logging.WARNING, "glm confirm failed — NO PAUSE", error=err)
        write_alert(cfg["listener"]["alerts_file"], "glm_unreachable_manual_check",
                    f"NPU flagged failure on {job or 'print'} but GLM confirm is "
                    f"unreachable — NOT paused, check manually",
                    {"frame": str(frame_path), "frame_url": frame_url,
                     "npu_verdict": npu_verdict, "detections": detections,
                     "error": err}, log)
        return 200, {"status": "glm_unreachable_no_pause", "frame": str(frame_path),
                     "frame_url": frame_url}

    action, why = decide(glm, cfg.getfloat("listener", "confirm_min_conf", fallback=0.6))
    log_event(log, logging.INFO, "glm confirm verdict", action=action, why=why,
              latency_s=glm_latency, frame=str(frame_path))
    with Path(cfg["listener"]["verdicts_file"]).open("a") as f:
        f.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "frame": str(frame_path), "mode": "glm_confirm", "action": action,
            "why": why, "glm_latency_s": glm_latency,
            "npu_verdict": npu_verdict, "glm_verdict": glm}) + "\n")

    if action != "pause":
        write_alert(cfg["listener"]["alerts_file"], "glm_disagreed_manual_check",
                    f"NPU flagged '{npu_verdict.get('state', '?')}' on "
                    f"{job or 'print'} but GLM disagrees — NOT paused, "
                    f"check manually",
                    {"frame": str(frame_path), "frame_url": frame_url,
                     "npu_verdict": npu_verdict, "detections": detections,
                     "glm_verdict": glm, "why": why}, log)
        return 200, {"status": "glm_disagreed_no_pause", "frame": str(frame_path),
                     "frame_url": frame_url, "glm": glm}

    # GLM confirmed -> the one and only write path
    if not cfg.getboolean("listener", "pause_enabled", fallback=True):
        write_alert(cfg["listener"]["alerts_file"], "auto_paused_dry_run",
                    f"DRY RUN: GLM confirmed failure on {job or 'print'} — "
                    f"pause NOT sent (pause_enabled=false)",
                    {"frame": str(frame_path), "frame_url": frame_url,
                     "npu_verdict": npu_verdict, "detections": detections,
                     "glm_verdict": glm, "why": why}, log)
        return 200, {"status": "confirmed_dry_run_no_pause",
                     "frame": str(frame_path), "frame_url": frame_url, "glm": glm}

    pause_ok, pause_detail = send_pause(mr_url, cfg["printer"].get(
        "pause_script", fallback="PAUSE"),
        cfg.getint("listener", "pause_verify_s", fallback=10), log)
    detail = {
        "frame": str(frame_path), "frame_url": frame_url,
        "npu_verdict": npu_verdict, "detections": detections,
        "glm_verdict": glm, "why": why, "job": job,
        "pause_result": "paused" if pause_ok else "pause_failed",
        "pause_detail": pause_detail,
        "resume_instruction": "Print auto-PAUSED. Check the Mainsail camera: "
                              "RESUME if false alarm, CANCEL_PRINT if real.",
    }
    if pause_ok:
        write_alert(cfg["listener"]["alerts_file"], "auto_paused",
                    f"AUTO-PAUSED {job or 'print'}: GLM confirmed NPU failure "
                    f"({npu_verdict.get('state', '?')}). Check Mainsail — "
                    f"RESUME if false alarm, CANCEL_PRINT if real.", detail, log)
        write_marker(cfg["listener"]["marker_file"], {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "frame": str(frame_path), "frame_url": frame_url, "job": job,
            "npu_verdict": npu_verdict, "glm_verdict": glm, "why": why})
        log_event(log, logging.WARNING, "AUTO-PAUSE sent + verified", job=job,
                  why=why, frame=str(frame_path))
    else:
        write_alert(cfg["listener"]["alerts_file"], "pause_failed_manual_check",
                    f"GLM confirmed failure on {job or 'print'} but PAUSE did "
                    f"NOT verify — print NOT safely paused, check NOW",
                    detail, log)
    return 200, {"status": "paused" if pause_ok else "pause_failed",
                 "frame": str(frame_path), "frame_url": frame_url,
                 "glm": glm, "pause_detail": pause_detail}


def make_handler(cfg, st, log):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/confirm":
                return self._reply(404, {"status": "not_found"})
            if not st.sem.acquire(blocking=False):
                return self._reply(503, {"status": "busy",
                                         "error": "another confirm in progress"})
            n = int(self.headers.get("Content-Length", 0))
            if n <= 0 or n > 20_000_000:
                st.sem.release()
                return self._reply(400, {"status": "bad_request",
                                         "error": f"bad content-length {n}"})
            try:
                req = json.loads(self.rfile.read(n).decode())
            except Exception:
                st.sem.release()
                return self._reply(400, {"status": "bad_request",
                                         "error": "body not JSON"})
            req["_token"] = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            try:
                code, resp = process_confirm(cfg, st, req,
                                             self.client_address[0], log)
            except Exception as e:
                log_event(log, logging.ERROR, "confirm handler crashed",
                          error=str(e)[:250])
                code, resp = 500, {"status": "internal_error",
                                   "error": str(e)[:200]}
            finally:
                st.sem.release()
            self._reply(code, resp)

        def do_GET(self):
            # failed-frame serving so alerts can carry a clickable LAN link
            m = re.fullmatch(r"/frame/([A-Za-z0-9_.-]+)", self.path)
            if m:
                root = Path(cfg["listener"]["pause_frame_dir"]).resolve()
                target = (root / m.group(1)).resolve()
                if target.parent != root or not target.is_file():
                    return self._reply(404, {"status": "not_found"})
                data = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path == "/health":
                return self._reply(200, {"status": "ok",
                                         "listener": "a3dp-confirm-listener"})
            return self._reply(404, {"status": "not_found"})

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            log.info("http %s", format % args)

    return Handler


def prune_pause_frames(frame_dir, retention_days, log):
    cutoff = time.time() - retention_days * 86400
    for f in Path(frame_dir).glob("*.jpg"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    args = ap.parse_args()

    cfg = configparser.ConfigParser(inline_comment_prefixes=("#",))
    cfg.read(args.config)
    frame_dir = Path(cfg["listener"]["pause_frame_dir"])
    base = frame_dir.parent
    frame_dir.mkdir(parents=True, exist_ok=True)
    (base / "logs").mkdir(parents=True, exist_ok=True)

    log = logging.getLogger("confirm-listener")
    if not log.handlers:
        rh = logging.handlers.RotatingFileHandler(
            base / "logs" / "confirm.log", maxBytes=5_000_000, backupCount=3)
        rh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(rh)
        log.setLevel(logging.INFO)

    # single instance (service restarts are serialized by systemd anyway)
    lock_fh = open(base / "confirm.lock", "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another listener is running; exiting", flush=True)
        return 0
    st = ConfirmState()

    port = cfg.getint("listener", "port", fallback=8765)
    bind = cfg.get("listener", "bind", fallback="0.0.0.0")
    log_event(log, logging.INFO, "confirm listener starting", bind=bind, port=port)

    prune_pause_frames(frame_dir, cfg.getint("listener", "frame_retention_days",
                                             fallback=7), log)
    handler = make_handler(cfg, st, log)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer((bind, port), handler) as httpd:
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
