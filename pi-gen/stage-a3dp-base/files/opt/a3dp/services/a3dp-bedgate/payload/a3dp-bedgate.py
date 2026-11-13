#!/usr/bin/env python3
"""a3dp-bedgate — bed-occupancy gate daemon for the Rift (a3dp525).

Companion to the machine-side gate macros (configs/bed-gate/bed-gate.cfg, live).
Contract: configs/bed-gate/GATE-CONTRACT.md — every behavior there is a safety
decision, not a preference. Read it before touching this file.

Responsibilities (coder side of the split):
  - Moonraker websocket + poll: detect job start (print_stats transition to
    "printing"; Moonraker v0.11.0 has no job_start webhook) -> hard gate:
    occupied + fresh + enabled + no override -> PAUSE + RESPOND TYPE=error +
    M117. NEVER auto-resume, NEVER auto-cancel. Escalate at 10 min, hourly.
  - REST on 127.0.0.1:8084, behind nginx :8083 (LAN allowlist):
      POST /api/occupancy  {occupied, confidence, reason, ts, frame}
      POST /api/override   {minutes, who}   (who mandatory, always logged)
      GET  /api/state      (also serves detection workers: toolhead, bed temp)
  - Heartbeat every <=120 s (60 s): bed_clear + ticks_since_update=0 via
    POST /printer/gcode/script. This heartbeat IS the fail-open lifeline.
  - Fail-open resets: startup, systemd ExecStop, detection-verdict staleness.

Warning layer (Moonraker v0.11.0 has NO announcements post API — verified 404):
  klippy console RESPOND TYPE=error/command + M117. klippy prompts
  (action:prompt_*) are template-only and CANNOT be raised from a raw gcode
  script — documented gap, console+M117 are the live surface.

Stdlib only. Unit tests: scripts/test_a3dp-bedgate.py.
"""

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MACHINE = "a3dp525"
GATE = "BED_GATE"
CHECK = "_BED_GATE_CHECK"


def log(msg):
    print(f"[bedgate {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------ config

@dataclass
class Config:
    # Public surface is nginx :8083 (LAN allowlist, see nginx-bedgate.conf);
    # the daemon itself binds the internal loopback port below.
    host: str = "127.0.0.1"
    port: int = 8084
    moonraker_http: str = "http://127.0.0.1:7125"
    ws_host: str = "127.0.0.1"
    ws_port: int = 7125
    ws_path: str = "/websocket"
    heartbeat_s: float = 60.0        # contract: <= 120
    verdict_ttl_s: float = 300.0     # detection worker silent this long -> verdict stale
    midprint_s: float = 120.0        # contract: mark bed occupied after 120 s of print
    pause_delay_s: float = 2.0       # contract: delay PAUSE ~2 s, skip if job died
    escalate_after_s: float = 600.0  # contract: 10 min
    escalate_repeat_s: float = 3600.0  # contract: hourly
    poll_s: float = 10.0             # status poll (also catches missed ws updates)
    event_log: str = os.path.expanduser(
        "~/printer_data/logs/a3dp-bedgate-events.jsonl")

    @classmethod
    def from_env(cls):
        c = cls()
        e = os.environ.get
        c.host = e("A3DP_BEDGATE_HOST", c.host)
        c.port = int(e("A3DP_BEDGATE_PORT", c.port))
        c.moonraker_http = e("A3DP_BEDGATE_MOONRAKER", c.moonraker_http)
        c.ws_host = e("A3DP_BEDGATE_WS_HOST", c.ws_host)
        c.ws_port = int(e("A3DP_BEDGATE_WS_PORT", c.ws_port))
        c.heartbeat_s = float(e("A3DP_BEDGATE_HEARTBEAT_S", c.heartbeat_s))
        c.verdict_ttl_s = float(e("A3DP_BEDGATE_VERDICT_TTL_S", c.verdict_ttl_s))
        c.midprint_s = float(e("A3DP_BEDGATE_MIDPRINT_S", c.midprint_s))
        c.pause_delay_s = float(e("A3DP_BEDGATE_PAUSE_DELAY_S", c.pause_delay_s))
        c.escalate_after_s = float(e("A3DP_BEDGATE_ESCALATE_AFTER_S", c.escalate_after_s))
        c.escalate_repeat_s = float(e("A3DP_BEDGATE_ESCALATE_REPEAT_S", c.escalate_repeat_s))
        c.poll_s = float(e("A3DP_BEDGATE_POLL_S", c.poll_s))
        c.event_log = e("A3DP_BEDGATE_EVENT_LOG", c.event_log)
        assert 0 < c.heartbeat_s <= 120, "heartbeat must be <= 120 s (contract)"
        return c


# --------------------------------------------------- gcode script builders
# Verified machine gotchas (GATE-CONTRACT top): SET_GCODE_VARIABLE VALUE goes
# through whitespace-split + ast.literal_eval after ONE outer quote layer is
# stripped -> strings must be double-wrapped VALUE='"text"' and contain NO
# spaces. Booleans/ints are bare.

def sanitize_token(s):
    """String values for klippy params: no spaces, no quotes, bounded length."""
    out = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in str(s))
    while "__" in out:
        out = out.replace("__", "_")
    out = out.strip("_")[:64]
    return out or "unknown"


def var_line(macro, variable, value):
    """SET_GCODE_VARIABLE line for bool/int values."""
    if isinstance(value, bool):
        lit = "True" if value else "False"
    elif isinstance(value, int):
        lit = str(value)
    else:
        raise TypeError(f"non-scalar value for {variable}: {value!r}")
    return f"SET_GCODE_VARIABLE MACRO={macro} VARIABLE={variable} VALUE={lit}"


def var_line_str(macro, variable, s):
    """SET_GCODE_VARIABLE line for string values (double-wrapped quotes)."""
    s = sanitize_token(s)
    return f"SET_GCODE_VARIABLE MACRO={macro} VARIABLE={variable} VALUE='\"{s}\"'"


def gate_write_script(bed_clear=None, reset_ticks=True, override=None):
    """One POST /printer/gcode/script body. override=(minutes, who) or None."""
    lines = []
    if bed_clear is not None:
        lines.append(var_line(GATE, "bed_clear", bool(bed_clear)))
    if reset_ticks:
        lines.append(var_line(GATE, "ticks_since_update", 0))
    if override is not None:
        minutes, who = override
        lines.append(var_line(GATE, "override_active", True))
        lines.append(var_line(GATE, "override_ticks_left", int(minutes)))
        lines.append(var_line_str(GATE, "override_by", f"daemon_{who}"))
    return "\n".join(lines)


def gate_decision(gate_vars):
    """Pure gate logic — mirrors bed-gate.cfg _BED_GATE_CHECK ordering.

    Returns one of: pass_disabled, pass_stale, pass_override, pass_clear, block.
    """
    if not gate_vars.get("gate_enabled", True):
        return "pass_disabled"
    ticks = int(gate_vars.get("ticks_since_update", 999))
    stale = int(gate_vars.get("stale_ticks", 5))
    if ticks > stale:
        return "pass_stale"
    if gate_vars.get("override_active", False):
        return "pass_override"
    if gate_vars.get("bed_clear", True):
        return "pass_clear"
    return "block"


def parse_occupancy(payload, now, max_skew_s=120.0):
    """Validate a detection verdict. Returns (verdict_dict|None, error|None)."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    occ = payload.get("occupied")
    if not isinstance(occ, bool):
        return None, "occupied must be a bool"
    conf = payload.get("confidence")
    if not isinstance(conf, (int, float)) or not (0.0 <= float(conf) <= 1.0):
        return None, "confidence must be 0.0-1.0"
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason:
        return None, "reason must be a non-empty string"
    ts = payload.get("ts")
    if not isinstance(ts, (int, float)):
        return None, "ts must be an epoch number"
    if abs(now - float(ts)) > max_skew_s:
        return None, f"ts skew {abs(now - float(ts)):.0f}s > {max_skew_s:.0f}s — fix worker clock"
    frame = payload.get("frame")
    if frame is not None and not isinstance(frame, str):
        return None, "frame must be a path string or null"
    return {"occupied": occ, "confidence": float(conf), "reason": reason,
            "ts": float(ts), "frame": frame}, None


# --------------------------------------------------------- moonraker client

class MoonError(Exception):
    def __init__(self, msg, timeout=False):
        super().__init__(msg)
        self.timeout = timeout  # True = client-side timeout (command may
        # still have been delivered and queued by klippy — do NOT blind-retry)


class Moon:
    """Moonraker HTTP client (LAN-open trusted_clients — no API key, by design)."""

    def __init__(self, base_url, timeout=10):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _req(self, method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as f:
                body = f.read().decode()
        except urllib.error.HTTPError as e:
            raise MoonError(f"{method} {path} -> HTTP {e.code}: {e.read().decode()[:200]}")
        except (socket.timeout, TimeoutError) as e:
            raise MoonError(f"{method} {path} -> timeout: {e}", timeout=True)
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)) or \
                    "timed out" in str(e.reason):
                raise MoonError(f"{method} {path} -> timeout: {e.reason}", timeout=True)
            raise MoonError(f"{method} {path} -> {type(e).__name__}: {e}")
        except Exception as e:
            raise MoonError(f"{method} {path} -> {type(e).__name__}: {e}")
        try:
            return json.loads(body)
        except ValueError:
            raise MoonError(f"{method} {path}: non-JSON response")

    def post_script(self, script):
        r = self._req("POST", "/printer/gcode/script", {"script": script})
        # klippy can still report errors inside a 200 envelope
        err = (r.get("error") if isinstance(r, dict) else None)
        if err:
            raise MoonError(f"gcode/script error: {json.dumps(err)[:200]}")
        return r

    def query(self, objects):
        # Moonraker format: each query-param NAME is an object name
        # (?print_stats&gcode_macro%20BED_GATE). Verified live: sending
        # "?objects=<name>" queries a nonexistent object literally named
        # "objects" and returns an empty status.
        qs = "&".join(urllib.parse.quote(o, safe="") for o in objects)
        r = self._req("GET", "/printer/objects/query?" + qs)
        return r.get("result", {}).get("status", {})

    def info(self):
        return self._req("GET", "/printer/info")


# ------------------------------------------------------- websocket client
# Minimal RFC6455 client, stdlib only. Just enough for Moonraker's JSON-RPC
# notification stream: text frames, ping/pong, close, fragmentation.

class WsClient:
    def __init__(self, host, port, path="/websocket", timeout=30):
        self.host, self.port, self.path = host, port, path
        self.timeout = timeout
        self.sock = None
        self.buf = b""

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {self.path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed during ws handshake")
            resp += chunk
        status = resp.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise ConnectionError(f"ws handshake refused: {status!r}")
        self.buf = resp.split(b"\r\n\r\n", 1)[1]
        return self

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("ws closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, opcode, data=b""):
        mask = os.urandom(4)
        n = len(data)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(hdr + mask + masked)

    def send_json(self, obj):
        self._send_frame(0x1, json.dumps(obj).encode())

    def ping(self):
        self._send_frame(0x9, b"hb")

    def recv_message(self, timeout=None):
        """Return one complete text message, or raise on close/timeout."""
        if timeout is not None:
            self.sock.settimeout(timeout)
        parts = []
        while True:
            h = self._read_exact(2)
            fin = h[0] & 0x80
            opcode = h[0] & 0x0F
            masked = h[1] & 0x80
            ln = h[1] & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else None
            data = self._read_exact(ln)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:            # ping -> pong
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:            # pong
                continue
            if opcode == 0x8:
                raise ConnectionError("ws close frame")
            if opcode == 0x1:
                parts = [data]
            elif opcode == 0x0:
                parts.append(data)
            else:
                continue                 # binary etc — ignore
            if fin:
                return b"".join(parts).decode(errors="replace")

    def close(self):
        try:
            self._send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


# ------------------------------------------------------------- event log

class EventLog:
    """JSONL event log with size rotation (keeps one .1 generation)."""

    def __init__(self, path, max_bytes=10 * 1024 * 1024):
        self.path = path
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)

    def add(self, etype, **kw):
        rec = {"ts": round(time.time(), 3), "type": etype}
        rec.update(kw)
        line = json.dumps(rec, default=str)
        with self.lock:
            try:
                if os.path.exists(self.path) and \
                        os.path.getsize(self.path) > self.max_bytes:
                    os.replace(self.path, self.path + ".1")
                with open(self.path, "a") as f:
                    f.write(line + "\n")
            except OSError as e:
                log(f"event log write failed: {e}")
        log(f"event {etype} {kw if kw else ''}")


# ------------------------------------------------------------------ gate

class BedGate:
    def __init__(self, cfg, moon, events, clock=time.time):
        self.cfg = cfg
        self.moon = moon
        self.events = events
        self.clock = clock
        self.lock = threading.RLock()
        self.bed_clear_written = None       # last value written to klippy
        self.verdict = None                 # last accepted detection verdict
        self.verdict_received = None        # daemon clock at receive
        self.midprint_hold = False          # job running > 120 s -> bed occupied
        self.job = None                     # {"name","started_at"}
        self.pause_tracking = None          # {"job","since","escalations"}
        self.gate_vars = None               # last known BED_GATE/_BED_GATE_CHECK vars
        self.print_stats = None
        self.pause_resume = None
        self.toolhead = None
        self.heater_bed = None
        self.moonraker_ok = False
        self.klippy_ok = False
        self.failopen_warned = False
        self._last_job_start = 0.0
        self._stopping = False

    # ------------------------------------------------------------- helpers

    def event(self, etype, **kw):
        self.events.add(etype, **kw)

    def _script(self, script, what):
        try:
            self.moon.post_script(script)
            self.moonraker_ok = True
            return True
        except MoonError as e:
            self.moonraker_ok = False
            log(f"{what} write FAILED (retried next beat): {e}")
            self.event("moonraker_write_fail", what=what, error=str(e)[:200])
            return False

    def console(self, level, msg):
        """klippy console message. level: 'error' (red) or 'command' (info)."""
        msg = msg.replace('"', "'")
        self._script(f'RESPOND TYPE={level} MSG="{msg}"', f"console({level})")

    def m117(self, msg):
        self._script("M117 " + msg, "m117")

    def write_fail_open(self, warn_msg):
        """The fail-open reset (startup / ExecStop / stale verdict)."""
        script = gate_write_script(bed_clear=True, reset_ticks=True)
        ok = self._script(script, "fail_open_reset")
        if ok:
            self.bed_clear_written = True
            self.failopen_warned = False
        self.console("command", warn_msg)
        self.event("failopen_reset", msg=warn_msg, written=ok)
        return ok

    def desired_bed_clear(self, now):
        """(value, reason) for the heartbeat. Fail-open when no fresh verdict."""
        with self.lock:
            if self.midprint_hold:
                return False, "midprint_occupied"
            v = self.verdict
            if v is not None and (now - self.verdict_received) <= self.cfg.verdict_ttl_s:
                return (not v["occupied"]), "verdict"
            return True, "failopen"

    def heartbeat_once(self):
        now = self.clock()
        value, reason = self.desired_bed_clear(now)
        with self.lock:
            changed = value != self.bed_clear_written
        script = gate_write_script(bed_clear=value, reset_ticks=True)
        ok = self._script(script, "heartbeat")
        if ok:
            with self.lock:
                self.bed_clear_written = value
            if changed and reason == "failopen" and not self.failopen_warned:
                self.failopen_warned = True
                self.console("command",
                             "BED GATE: detection verdict stale — passing (fail-open)")
                self.event("verdict_stale_failopen")
            if changed and reason != "failopen":
                self.failopen_warned = False
        return ok

    # ------------------------------------------------------- occupancy API

    def handle_occupancy(self, payload):
        now = self.clock()
        verdict, err = parse_occupancy(payload, now)
        if err:
            return 400, {"ok": False, "msg": err}
        if verdict["confidence"] < 0.5:
            # DETECTION-SPEC: below 0.5 -> reason low_confidence, no-verdict
            # (stale path, fail open). Never write bed_clear from it.
            self.event("verdict_rejected", reason=verdict["reason"],
                       confidence=verdict["confidence"])
            return 200, {"ok": True, "accepted": False,
                         "msg": "low_confidence — treated as no verdict"}
        with self.lock:
            self.verdict = verdict
            self.verdict_received = now
            desired = not verdict["occupied"]
        script = gate_write_script(bed_clear=desired, reset_ticks=True)
        ok = self._script(script, "occupancy")
        if ok:
            with self.lock:
                self.bed_clear_written = desired
            self.failopen_warned = False
        self.event("verdict", occupied=verdict["occupied"],
                   confidence=verdict["confidence"], reason=verdict["reason"],
                   frame=verdict["frame"], written=ok)
        return 200, {"ok": True, "accepted": True, "bed_clear": desired,
                     "written": ok}

    def handle_override(self, minutes, who):
        if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0 \
                or minutes > 24 * 60:
            return 400, {"ok": False, "msg": "minutes must be int 1..1440"}
        if not isinstance(who, str) or not who.strip():
            return 400, {"ok": False, "msg": "who is mandatory (logged)"}
        who = sanitize_token(who)
        script = gate_write_script(reset_ticks=True, override=(minutes, who))
        ok = self._script(script, "override")
        self.event("override_api", minutes=minutes, who=who, written=ok)
        if ok:
            self.console("command",
                         f"BED GATE: override active for {minutes} min (daemon_{who})")
        return (200 if ok else 502), {"ok": ok,
                                      "msg": "override written" if ok else
                                      "moonraker write failed — not active"}

    # --------------------------------------------------- status transitions

    def apply_status(self, status):
        """Single funnel for ws notify_status_update AND the poll thread."""
        now = self.clock()
        with self.lock:
            if "gcode_macro BED_GATE" in status:
                g = status["gcode_macro BED_GATE"]
                prev = self.gate_vars or {}
                if "override_active" in g and "override_active" in prev and \
                        bool(g["override_active"]) != bool(prev["override_active"]):
                    if g["override_active"]:
                        self.event("override_console", who=g.get("override_by"),
                                   minutes=g.get("override_ticks_left"))
                        self.console("command",
                                     "BED GATE: console override logged "
                                     f"({g.get('override_by')})")
                    else:
                        self.event("override_expired")
                merged = dict(prev)
                merged.update(g)
                self.gate_vars = merged
            if "gcode_macro _BED_GATE_CHECK" in status:
                merged = dict(self.gate_vars or {})
                merged["gate_enabled"] = \
                    status["gcode_macro _BED_GATE_CHECK"].get("gate_enabled", True)
                self.gate_vars = merged
            ps = status.get("print_stats")
            old_state = (self.print_stats or {}).get("state")
            if ps is not None:
                merged_ps = dict(self.print_stats or {})
                merged_ps.update(ps)
                self.print_stats = merged_ps
                # Moonraker sends CHANGED FIELDS ONLY in notify_status_update —
                # a print_stats dict without "state" is not a state change
                if "state" in ps:
                    new_state = ps["state"]
                    if new_state == "printing" and \
                            old_state not in ("printing", "paused"):
                        # NEW job only. paused->printing is a RESUME of the
                        # same job — re-gating it would trap the human (the
                        # RESUME is the logged override, never re-pause).
                        # old_state None included: daemon start / klippy
                        # restart while a job is already running.
                        if now - self._last_job_start > 20:  # ws + poll dedupe
                            self._last_job_start = now
                            self.job = {"name": ps.get("filename", ""),
                                        "started_at": now, "override": False}
                            threading.Thread(
                                target=self._job_start_worker,
                                args=(ps.get("filename", ""),), daemon=True).start()
                    elif old_state in ("printing", "paused") and \
                            new_state not in ("printing", "paused"):
                        # job end from running OR paused (cancel-while-paused)
                        self._job_end(new_state, ps)
                    elif new_state == "printing" and not self.midprint_hold:
                        dur = float(ps.get("print_duration") or 0.0)
                        if dur > self.cfg.midprint_s:
                            self.midprint_hold = True
                            ok = self._script(
                                gate_write_script(bed_clear=False, reset_ticks=True),
                                "midprint_mark")
                            if ok:
                                self.bed_clear_written = False
                            self.event("midprint_mark", duration=dur, written=ok)
                            self.console("command",
                                         "BED GATE: print >120 s — bed marked "
                                         "occupied (mid-print homing/probe/mesh "
                                         "blocked)")
            pr = status.get("pause_resume")
            if pr is not None:
                if "is_paused" in pr:
                    was = (self.pause_resume or {}).get("is_paused")
                    if was is True and pr["is_paused"] is False \
                            and self.pause_tracking:
                        p = self.pause_tracking
                        self.pause_tracking = None
                        with self.lock:
                            if self.job:
                                self.job["override"] = True
                        self.event("resume_override", job=p["job"],
                                   paused_s=round(now - p["since"], 1),
                                   escalations=p["escalations"])
                        self.console("command",
                                     "BED GATE: RESUME logged as human override — "
                                     "verdict checking continues")
                merged_pr = dict(self.pause_resume or {})
                merged_pr.update(pr)
                self.pause_resume = merged_pr
            for name in ("toolhead", "heater_bed"):
                if name in status:
                    setattr(self, name, status[name])

    def _job_end(self, new_state, ps):
        self.event("job_end", state=new_state, name=(self.job or {}).get("name"),
                   duration=ps.get("print_duration"))
        self.job = None
        self.pause_tracking = None
        self.midprint_hold = False
        # The pre-start verdict is now history (the part may still be on the
        # bed). Clear it so the heartbeat holds fail-open until a FRESH idle
        # verdict arrives — never assert clear from a guess.
        self.verdict = None
        self.verdict_received = None

    # ------------------------------------------------------- job hard gate

    def _job_start_worker(self, filename):
        self.event("job_start", name=filename)
        time.sleep(self.cfg.pause_delay_s)   # contract: delay ~2 s
        try:
            st = self.moon.query(["print_stats", "gcode_macro BED_GATE",
                                  "gcode_macro _BED_GATE_CHECK", "pause_resume"])
        except MoonError as e:
            log(f"job_start gate: state query failed: {e}")
            self.event("job_start_query_fail", error=str(e)[:200])
            return
        self.apply_status(st)
        ps = st.get("print_stats", {})
        if ps.get("state") != "printing":
            # job died between event and now (macro preflight aborts it) —
            # contract: skip, don't add noise
            self.event("job_start_skip", why="job_not_active",
                       observed_state=ps.get("state"))
            return
        gate = dict(st.get("gcode_macro BED_GATE") or {})
        gate["gate_enabled"] = (st.get("gcode_macro _BED_GATE_CHECK") or {}) \
            .get("gate_enabled", True)
        with self.lock:
            if self.job and self.job.get("override"):
                # this job was already paused once and a human RESUMEd it —
                # the RESUME was the logged override; never re-pause
                self.event("job_start_pass", why="resume_override_granted")
                return
        d = gate_decision(gate)
        if d == "pass_disabled":
            self.event("job_start_pass", why="kill_switch")
            return
        if d == "pass_stale":
            self.console("command",
                         "BED GATE: daemon state stale — job allowed (fail-open)")
            self.event("job_start_pass", why="stale_failopen")
            return
        if d == "pass_override":
            self.event("job_start_pass", why="override",
                       who=gate.get("override_by"))
            self.console("command",
                         f"BED GATE: override active ({gate.get('override_by')}) "
                         "— job allowed (logged)")
            return
        if d == "pass_clear":
            return
        # d == "block": occupied + fresh + enabled + no override
        jobname = (self.job or {}).get("name") or filename
        paused = self._pause_with_retry()
        with self.lock:
            self.pause_tracking = {"job": jobname, "since": self.clock(),
                                   "escalations": 0}
        msg = ("BED GATE: bed occupied — job paused. Remove part, then RESUME "
               "(logged override)")
        self.console("error", msg)
        self.m117("Bed occupied - job paused. Remove part, then RESUME (logged override)")
        self.event("gate_pause", job=jobname, pause_written=paused,
                   verdict=self.verdict)

    def _wait_paused(self, seconds):
        """Poll pause_resume — moonraker serves cached status even while
        klippy is busy executing (verified live during homing/G4)."""
        deadline = self.clock() + seconds
        while self.clock() < deadline:
            time.sleep(2)
            try:
                st = self.moon.query(["pause_resume"])
                if (st.get("pause_resume") or {}).get("is_paused"):
                    return True
            except Exception:
                pass
        return False

    def _pause_with_retry(self):
        """Hard-gate PAUSE. Klippy cannot accept gcode while a blocking
        operation (homing, long G4, macro) runs: a client-side timeout does
        NOT mean the command was lost — it is queued and runs when klippy is
        free. Re-sending on timeout would DOUBLE-PAUSE, and this machine's
        PAUSE macro parks unconditionally (double park breaks RESUME restore,
        verified live). So: timeout -> poll is_paused; definitive failure ->
        retry."""
        for attempt in range(3):
            try:
                self.moon.post_script("PAUSE")
                self.moonraker_ok = True
                return True
            except MoonError as e:
                self.moonraker_ok = False
                self.event("moonraker_write_fail", what="gate_pause",
                           error=str(e)[:200])
                if e.timeout:
                    if self._wait_paused(30):
                        self.moonraker_ok = True
                        return True
                else:
                    time.sleep(3)
            except Exception as e:
                log(f"gate_pause unexpected error: {e}")
                time.sleep(3)
        return False

    def escalate_once(self, k):
        p = self.pause_tracking
        if not p:
            return
        msg = (f"BED GATE: bed STILL occupied — job paused {k}x "
               f"{int(self.cfg.escalate_after_s / 60)}min+ now. Remove part, "
               "then RESUME (logged override)")
        self.console("error", msg)
        self.m117("Bed STILL occupied - paused. Remove part, then RESUME (logged override)")
        p["escalations"] = k
        self.event("gate_escalation", job=p["job"], n=k)

    def escalation_sweep(self, now):
        with self.lock:
            p = self.pause_tracking
            if not p:
                return
            state = (self.print_stats or {}).get("state")
            if state != "paused":
                return  # resume/end transitions handle cleanup
            since = now - p["since"]
            if since < self.cfg.escalate_after_s:
                return
            k = int((since - self.cfg.escalate_after_s)
                    // self.cfg.escalate_repeat_s) + 1
            if k > p["escalations"]:
                self.escalate_once(k)

    # ------------------------------------------------------- klippy events

    def on_klippy_disconnected(self):
        self.klippy_ok = False
        self.event("klippy_disconnected")

    def on_klippy_connected(self):
        self.klippy_ok = True
        self.event("klippy_reconnected")
        # klippy restart resets macros to fail-open defaults; re-sync NOW
        # (contract: daemon re-syncs within 120 s) and refresh full state.
        self.heartbeat_once()
        self.poll_once()

    # ----------------------------------------------------------- polling

    def poll_once(self):
        """Full-object poll — backup for missed ws updates + /api/state data."""
        try:
            st = self.moon.query(["print_stats", "pause_resume",
                                  "gcode_macro BED_GATE",
                                  "gcode_macro _BED_GATE_CHECK",
                                  "toolhead", "heater_bed"])
            self.moonraker_ok = True
            self.klippy_ok = True
        except MoonError as e:
            self.moonraker_ok = False
            self.klippy_ok = False
            return
        self.apply_status(st)

    # -------------------------------------------------------- state dump

    def state_dump(self):
        now = self.clock()
        with self.lock:
            v = dict(self.verdict) if self.verdict else None
            if v is not None:
                v["age_s"] = round(now - self.verdict_received, 1)
                v["fresh"] = v["age_s"] <= self.cfg.verdict_ttl_s
            return {
                "ok": True,
                "machine": MACHINE,
                "now": now,
                "config": {
                    "heartbeat_s": self.cfg.heartbeat_s,
                    "verdict_ttl_s": self.cfg.verdict_ttl_s,
                    "midprint_s": self.cfg.midprint_s,
                    "pause_delay_s": self.cfg.pause_delay_s,
                    "escalate_after_s": self.cfg.escalate_after_s,
                    "escalate_repeat_s": self.cfg.escalate_repeat_s,
                },
                "moonraker_ok": self.moonraker_ok,
                "klippy_ok": self.klippy_ok,
                "bed_clear_written": self.bed_clear_written,
                "midprint_hold": self.midprint_hold,
                "verdict": v,
                "gate_vars": self.gate_vars,
                "print_stats": self.print_stats,
                "pause_resume": self.pause_resume,
                "pause_tracking": self.pause_tracking,
                "job": self.job,
                "toolhead": self.toolhead,
                "heater_bed": self.heater_bed,
            }


# ------------------------------------------------------------ web layer

_PAGE = """<!doctype html><html><head><title>a3dp-bedgate</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>body{font-family:monospace;max-width:860px;margin:2rem auto;padding:0 1rem;background:#111;color:#ddd}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #444;padding:.3rem;text-align:left;font-size:.85rem}
input,button{padding:.3rem .5rem;background:#222;color:#ddd;border:1px solid #555}
#state{white-space:pre-wrap;font-size:.8rem;background:#181818;padding:.8rem;border:1px solid #333}</style></head><body>
<h1>a3dp-bedgate — %MACHINE%</h1>
<p>Fail-open gate: no fresh verdict = pass. Override = always human, always logged.</p>
<div><button onclick=refresh()>refresh</button>
<input id=min type=number value=10 min=1 style=width:5em> min
<input id=who placeholder="who (required)" style=width:12em>
<button onclick=override()>override</button></div>
<pre id=state>loading…</pre>
<script>
async function refresh(){
 const s=await (await fetch('/api/state')).json();
 document.getElementById('state').textContent=JSON.stringify(s,null,1);}
async function override(){
 const who=document.getElementById('who').value.trim();
 if(!who){alert('who is required');return;}
 const r=await fetch('/api/override',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({minutes:+document.getElementById('min').value,who})});
 const j=await r.json();alert(j.ok?'OK':'FAILED: '+j.msg);refresh();}
refresh();setInterval(refresh,5000);
</script></body></html>"""


def make_handler(gate):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length", 0))
            if n > 65536:
                raise ValueError("body too large")
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw or b"{}")

        def do_GET(self):
            if self.path == "/api/state":
                self._send(200, gate.state_dump())
            elif self.path == "/":
                self._send(200, _PAGE.replace("%MACHINE%", MACHINE).encode(),
                           "text/html; charset=utf-8")
            else:
                self._send(404, {"ok": False, "msg": "not found"})

        def do_POST(self):
            try:
                if self.path == "/api/occupancy":
                    code, resp = gate.handle_occupancy(self._body())
                    self._send(code, resp)
                elif self.path == "/api/override":
                    b = self._body()
                    code, resp = gate.handle_override(b.get("minutes"),
                                                      b.get("who"))
                    self._send(code, resp)
                else:
                    self._send(404, {"ok": False, "msg": "not found"})
            except (ValueError, json.JSONDecodeError) as e:
                self._send(400, {"ok": False, "msg": str(e)})

        def log_message(self, fmt, *args):
            pass  # journald stays quiet; we log events ourselves

    return Handler


# --------------------------------------------------------------- threads

def ws_loop(gate, stop):
    """Moonraker websocket: subscribe + notifications + reconnect/backoff."""
    backoff = 1.0
    while not stop.is_set():
        sub_id = 2
        try:
            ws = WsClient(gate.cfg.ws_host, gate.cfg.ws_port, gate.cfg.ws_path).connect()
            ws.send_json({"jsonrpc": "2.0", "method": "printer.objects.subscribe",
                          "params": {"objects": {
                              "print_stats": None, "pause_resume": None,
                              "gcode_macro BED_GATE": None,
                              "gcode_macro _BED_GATE_CHECK": None,
                              "toolhead": None, "heater_bed": None}},
                          "id": sub_id})
            log("ws connected + subscribed")
            backoff = 1.0
            last_ping = time.time()
            quiet = 0
            while not stop.is_set():
                try:
                    msg = ws.recv_message(timeout=30)
                    quiet = 0
                except socket.timeout:
                    quiet += 1
                    if time.time() - last_ping > 25:
                        ws.ping()
                        last_ping = time.time()
                    if quiet >= 4:            # no frame ~2 min: dead conn
                        raise ConnectionError("ws quiet timeout")
                    continue
                try:
                    m = json.loads(msg)
                except ValueError:
                    continue
                if m.get("id") == sub_id:
                    st = (m.get("result") or {}).get("status")
                    if st:
                        gate.apply_status(st)
                    continue
                method = m.get("method", "")
                if method == "notify_status_update":
                    params = m.get("params") or [{}]
                    if params and isinstance(params[0], dict):
                        gate.apply_status(params[0])
                elif method == "notify_klippy_disconnected":
                    gate.on_klippy_disconnected()
                elif method == "notify_klippy_connected" or \
                        method == "notify_klippy_ready":
                    gate.on_klippy_connected()
            ws.close()
        except Exception as e:
            if stop.is_set():
                return
            log(f"ws down ({type(e).__name__}: {e}) — reconnect in {backoff:.0f}s")
            gate.klippy_ok = False
            stop.wait(backoff)
            backoff = min(backoff * 2, 30)


def heartbeat_loop(gate, stop):
    while not stop.wait(gate.cfg.heartbeat_s):
        try:
            gate.heartbeat_once()
        except Exception as e:
            log(f"heartbeat error: {e}")
            gate.event("heartbeat_error", error=str(e)[:200])


def poll_loop(gate, stop):
    while not stop.wait(gate.cfg.poll_s):
        try:
            gate.poll_once()
        except Exception as e:
            log(f"poll error: {e}")


def escalation_loop(gate, stop):
    while not stop.wait(15):
        try:
            gate.escalation_sweep(gate.clock())
        except Exception as e:
            log(f"escalation sweep error: {e}")


# ------------------------------------------------------------------ main

def exec_stop_reset(cfg):
    """systemd ExecStop: write fail-open state + console warning, then exit."""
    moon = Moon(cfg.moonraker_http)
    gate = BedGate(cfg, moon, EventLog(cfg.event_log))
    deadline = time.time() + 12
    ok = False
    while time.time() < deadline:
        try:
            if gate.write_fail_open("bed gate offline — passing (fail-open)"):
                ok = True
                break
        except Exception as e:
            log(f"exec-stop reset error: {e}")
        time.sleep(2)
    if not ok:
        log("exec-stop reset could not reach moonraker — machine-side tick "
            "counter will fail open within 5 min")
    return 0


def main():
    if "--exec-stop-reset" in sys.argv:
        return exec_stop_reset(Config.from_env())

    cfg = Config.from_env()
    events = EventLog(cfg.event_log)
    moon = Moon(cfg.moonraker_http)
    gate = BedGate(cfg, moon, events)

    # Startup: fail-open reset BEFORE the API port opens (contract §2).
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if gate.write_fail_open(
                    "bed gate online — state reset (fail-open until fresh verdict)"):
                break
        except Exception as e:
            log(f"startup reset error: {e}")
        time.sleep(2)
    else:
        log("startup reset could not reach moonraker in 30 s — machine-side "
            "defaults are fail-open (ticks=999); heartbeat will resync")

    stop = threading.Event()
    threads = [
        threading.Thread(target=ws_loop, args=(gate, stop), daemon=True),
        threading.Thread(target=heartbeat_loop, args=(gate, stop), daemon=True),
        threading.Thread(target=poll_loop, args=(gate, stop), daemon=True),
        threading.Thread(target=escalation_loop, args=(gate, stop), daemon=True),
    ]
    for t in threads:
        t.start()

    import signal
    stopping = {"done": False}

    def on_term(signum, frame):
        if stopping["done"]:
            return
        stopping["done"] = True
        log("SIGTERM — writing fail-open state before exit")
        stop.set()
        # network I/O must not run inside the signal handler; the ExecStop
        # path already wrote the reset, this is the belt-and-braces for a
        # direct kill
        threading.Thread(target=gate.write_fail_open,
                         args=("bed gate offline — passing (fail-open)",),
                         daemon=True).start()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    srv = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(gate))
    log(f"a3dp-bedgate listening on {cfg.host}:{cfg.port} "
        f"(moonraker {cfg.moonraker_http}, heartbeat {cfg.heartbeat_s}s, "
        f"verdict TTL {cfg.verdict_ttl_s}s)")
    events.add("startup", port=cfg.port)
    signal.signal(signal.SIGTERM, on_term)
    try:
        srv.serve_forever()
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
