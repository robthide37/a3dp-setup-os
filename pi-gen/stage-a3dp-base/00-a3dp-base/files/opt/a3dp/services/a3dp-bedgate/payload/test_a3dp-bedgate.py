#!/usr/bin/env python3
"""Unit tests for a3dp-bedgate (pure logic + threaded integration, no hardware).

The contract's fail-open checklist (GATE-CONTRACT §6) maps to tests as follows:
  daemon killed -9      -> live test on the machine (nothing can write)
  systemd stop          -> test_exec_stop_reset + live ExecStop test
  detection worker dead -> test_verdict_stale_failopen (+ live TTL test)
  klippy restart        -> test_klippy_reconnect_resync (+ live restart test)
  moonraker down        -> test_moonraker_down_verdict_retained (+ live verify)

Run: python3 scripts/test_a3dp-bedgate.py
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_spec = importlib.util.spec_from_file_location(
    "a3dp_bedgate", __file__.rsplit("/", 1)[0] + "/a3dp-bedgate.py")
bg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bg)


# ------------------------------------------------------------ fake moonraker

class FakeMoonHandler(BaseHTTPRequestHandler):
    server_version = "FakeMoonraker/0"

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path == "/printer/gcode/script":
            if self.fk.down:
                return self._json(503, {"error": {"message": "moonraker down"}})
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            self.fk._record_script(body.get("script", ""))
            return self._json(200, {"result": {"status": "ok"}})
        return self._json(404, {"error": "not found"})

    def do_GET(self):
        if self.path.startswith("/printer/objects/query"):
            if self.fk.down:
                return self._json(503, {"error": {"message": "moonraker down"}})
            import urllib.parse
            # Moonraker format: object names are the query-param NAMES,
            # valueless (e.g. ?print_stats&gcode_macro%20BED_GATE)
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query,
                                       keep_blank_values=True)
            objs = list(qs.keys())
            if not objs or any(o == "objects" for o in objs):
                return self._json(200, {"result": {"status": {}}})  # wrong format
            status = {o: self.fk.state[o] for o in objs if o in self.fk.state}
            return self._json(200, {"result": {"status": status}})
        return self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        pass


class FakeMoonServer:
    def __init__(self):
        self.scripts = []
        self.state = {}
        self.down = False
        self.timeout_next_post = False   # simulate klippy busy: PAUSE queued
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_cls())
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def _handler_cls(self):
        fk = self

        class H(FakeMoonHandler):
            pass

        H.fk = fk
        return H

    def _record_script(self, script):
        self.scripts.append(script)
        if self.timeout_next_post and "PAUSE" in script:
            self.timeout_next_post = False
            # the command WAS delivered; klippy runs it when free. Respond
            # SLOWLY (> the client's 10 s timeout) so the client sees a
            # socket timeout — exactly like klippy busy on a blocking op.
            t = threading.Timer(1.0, lambda: self.state["pause_resume"]
                                .update({"is_paused": True}))
            t.daemon = True
            t.start()
            time.sleep(11)
        return None

    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()

    def reset(self):
        self.scripts.clear()
        self.down = False

    def set_gate(self, bed_clear=True, ticks=0, stale=5, override_active=False,
                 override_by="", override_ticks_left=0, gate_enabled=True):
        self.state["gcode_macro BED_GATE"] = {
            "bed_clear": bed_clear, "ticks_since_update": ticks,
            "stale_ticks": stale, "override_active": override_active,
            "override_by": override_by, "override_ticks_left": override_ticks_left,
        }
        self.state["gcode_macro _BED_GATE_CHECK"] = {"gate_enabled": gate_enabled}

    def set_printing(self, state="standby", duration=0.0, filename=""):
        self.state["print_stats"] = {"state": state, "print_duration": duration,
                                     "filename": filename}
        self.state["pause_resume"] = {"is_paused": False}


def short_cfg(tmp):
    return bg.Config(
        host="127.0.0.1", port=0, heartbeat_s=0.2, verdict_ttl_s=0.6,
        midprint_s=1.0, pause_delay_s=0.15, escalate_after_s=0.8,
        escalate_repeat_s=0.4, poll_s=0.2,
        event_log=f"{tmp}/events.jsonl")


def make_gate(fk, cfg):
    moon = bg.Moon(fk.url())
    events = bg.EventLog(cfg.event_log)
    return bg.BedGate(cfg, moon, events)


def rest_client(port, method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers={"Content-Type": "application/json"},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as f:
            return f.status, json.loads(f.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def wait_for(fn, timeout=3.0, step=0.02):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(step)
    return False


# ------------------------------------------------------------------ tests

class TestGcodeBuilders(unittest.TestCase):
    def test_string_double_wrapped_no_spaces(self):
        line = bg.var_line_str("BED_GATE", "override_by", "daemon_robs phone")
        self.assertEqual(
            line, "SET_GCODE_VARIABLE MACRO=BED_GATE VARIABLE=override_by "
                  "VALUE='\"daemon_robs_phone\"'")
        self.assertNotIn("  ", line)
        # the VALUE= param contains no bare spaces
        self.assertNotIn("VALUE= ", line)

    def test_bool_int_bare(self):
        self.assertEqual(bg.var_line("BED_GATE", "bed_clear", True),
                         "SET_GCODE_VARIABLE MACRO=BED_GATE VARIABLE=bed_clear VALUE=True")
        self.assertEqual(bg.var_line("BED_GATE", "ticks_since_update", 0),
                         "SET_GCODE_VARIABLE MACRO=BED_GATE VARIABLE=ticks_since_update VALUE=0")

    def test_gate_write_script_occupancy(self):
        s = bg.gate_write_script(bed_clear=False, reset_ticks=True)
        self.assertEqual(
            s, "SET_GCODE_VARIABLE MACRO=BED_GATE VARIABLE=bed_clear VALUE=False\n"
               "SET_GCODE_VARIABLE MACRO=BED_GATE VARIABLE=ticks_since_update VALUE=0")

    def test_gate_write_script_override(self):
        s = bg.gate_write_script(reset_ticks=True, override=(10, "robs phone"))
        self.assertIn("VARIABLE=override_active VALUE=True", s)
        self.assertIn("VARIABLE=override_ticks_left VALUE=10", s)
        self.assertIn("VARIABLE=override_by VALUE='\"daemon_robs_phone\"'", s)

    def test_sanitize_token(self):
        self.assertEqual(bg.sanitize_token('robs "phone"  x'), "robs_phone_x")
        self.assertEqual(bg.sanitize_token(""), "unknown")


class TestGateDecision(unittest.TestCase):
    def test_ordering(self):
        base = {"gate_enabled": True, "ticks_since_update": 1, "stale_ticks": 5,
                "override_active": False, "bed_clear": True}
        self.assertEqual(bg.gate_decision(base), "pass_clear")
        self.assertEqual(bg.gate_decision({**base, "bed_clear": False}), "block")
        self.assertEqual(bg.gate_decision(
            {**base, "bed_clear": False, "override_active": True}), "pass_override")
        self.assertEqual(bg.gate_decision(
            {**base, "bed_clear": False, "ticks_since_update": 6}), "pass_stale")
        self.assertEqual(bg.gate_decision(
            {**base, "bed_clear": False, "gate_enabled": False}), "pass_disabled")
        # never-written variables default fail-open (stale = pass)
        self.assertEqual(bg.gate_decision({}), "pass_stale")


class TestParseOccupancy(unittest.TestCase):
    def test_valid(self):
        v, err = bg.parse_occupancy(
            {"occupied": True, "confidence": 0.9, "reason": "blob", "ts": time.time(),
             "frame": "/tmp/f.jpg"}, time.time())
        self.assertIsNone(err)
        self.assertTrue(v["occupied"])

    def test_bad_fields(self):
        now = time.time()
        for bad in [{}, {"occupied": "yes"}, {"occupied": True},
                    {"occupied": True, "confidence": 2},
                    {"occupied": True, "confidence": 0.9},
                    {"occupied": True, "confidence": 0.9, "reason": ""},
                    {"occupied": True, "confidence": 0.9, "reason": "x",
                     "ts": now - 500}]:
            v, err = bg.parse_occupancy(bad, now)
            self.assertIsNone(v)
            self.assertTrue(err)


class TestDesiredBedClear(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = short_cfg(self.tmp)
        self.fk = FakeMoonServer()
        self.g = make_gate(self.fk, self.cfg)

    def tearDown(self):
        self.fk.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_verdict_failopen(self):
        v, why = self.g.desired_bed_clear(time.time())
        self.assertEqual((v, why), (True, "failopen"))

    def test_fresh_verdict_governs(self):
        self.g.verdict = {"occupied": True, "confidence": 0.9, "reason": "blob",
                          "ts": 0, "frame": None}
        self.g.verdict_received = time.time()
        self.assertEqual(self.g.desired_bed_clear(time.time()), (False, "verdict"))
        self.g.verdict["occupied"] = False
        self.assertEqual(self.g.desired_bed_clear(time.time()), (True, "verdict"))

    def test_stale_verdict_failopen(self):
        self.g.verdict = {"occupied": True, "confidence": 0.9, "reason": "blob",
                          "ts": 0, "frame": None}
        self.g.verdict_received = time.time() - self.cfg.verdict_ttl_s - 1
        self.assertEqual(self.g.desired_bed_clear(time.time()), (True, "failopen"))

    def test_midprint_hold_wins(self):
        self.g.verdict = {"occupied": False, "confidence": 0.9, "reason": "clear",
                          "ts": 0, "frame": None}
        self.g.verdict_received = time.time()
        self.g.midprint_hold = True
        self.assertEqual(self.g.desired_bed_clear(time.time()),
                         (False, "midprint_occupied"))


class TestIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = short_cfg(self.tmp)
        self.fk = FakeMoonServer()
        self.g = make_gate(self.fk, self.cfg)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), bg.make_handler(self.g))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.fk.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scripts_join(self):
        return "\n".join(self.fk.scripts)

    # --- occupancy ingest

    def test_occupancy_accept_writes_state(self):
        code, resp = rest_client(self.port, "POST", "/api/occupancy",
                                 {"occupied": True, "confidence": 0.9,
                                  "reason": "blob_detected", "ts": time.time()})
        self.assertEqual(code, 200)
        self.assertTrue(resp["accepted"])
        self.assertIn("VARIABLE=bed_clear VALUE=False", self.scripts_join())
        self.assertIn("VARIABLE=ticks_since_update VALUE=0", self.scripts_join())
        self.assertEqual(self.g.bed_clear_written, False)

    def test_occupancy_clear_writes_true(self):
        code, resp = rest_client(self.port, "POST", "/api/occupancy",
                                 {"occupied": False, "confidence": 0.95,
                                  "reason": "empty", "ts": time.time()})
        self.assertTrue(resp["accepted"])
        self.assertIn("VARIABLE=bed_clear VALUE=True", self.scripts_join())

    def test_occupancy_low_confidence_no_write(self):
        n0 = len(self.fk.scripts)
        code, resp = rest_client(self.port, "POST", "/api/occupancy",
                                 {"occupied": True, "confidence": 0.3,
                                  "reason": "low_confidence", "ts": time.time()})
        self.assertEqual((code, resp["accepted"]), (200, False))
        self.assertEqual(len(self.fk.scripts), n0)  # nothing written to klippy
        self.assertIsNone(self.g.verdict)

    def test_occupancy_bad_ts_rejected(self):
        code, resp = rest_client(self.port, "POST", "/api/occupancy",
                                 {"occupied": True, "confidence": 0.9,
                                  "reason": "x", "ts": time.time() - 9999})
        self.assertEqual(code, 400)

    # --- fail-open: worker dead -> verdict stale -> fail open, never False

    def test_verdict_stale_failopen(self):
        rest_client(self.port, "POST", "/api/occupancy",
                    {"occupied": True, "confidence": 0.9, "reason": "blob",
                     "ts": time.time()})
        n0 = len(self.fk.scripts)
        time.sleep(self.cfg.verdict_ttl_s + 0.4)
        self.g.heartbeat_once()
        self.assertIn("VARIABLE=bed_clear VALUE=True", self.scripts_join())
        self.assertGreater(len(self.fk.scripts), n0)
        events = open(self.cfg.event_log).read()
        self.assertIn("verdict_stale_failopen", events)

    # --- override API

    def test_override_api(self):
        code, resp = rest_client(self.port, "POST", "/api/override",
                                 {"minutes": 10, "who": "robs phone"})
        self.assertEqual(code, 200)
        s = self.scripts_join()
        self.assertIn("VARIABLE=override_active VALUE=True", s)
        self.assertIn("VARIABLE=override_ticks_left VALUE=10", s)
        self.assertIn("VARIABLE=override_by VALUE='\"daemon_robs_phone\"'", s)
        events = open(self.cfg.event_log).read()
        self.assertIn("override_api", events)

    def test_override_requires_who(self):
        code, _ = rest_client(self.port, "POST", "/api/override", {"minutes": 5})
        self.assertEqual(code, 400)
        code, _ = rest_client(self.port, "POST", "/api/override",
                              {"minutes": 0, "who": "x"})
        self.assertEqual(code, 400)

    # --- state API

    def test_state_dump(self):
        code, s = rest_client(self.port, "GET", "/api/state")
        self.assertEqual(code, 200)
        self.assertTrue(s["ok"])
        self.assertEqual(s["machine"], "a3dp525")
        for k in ("config", "moonraker_ok", "bed_clear_written", "verdict",
                  "gate_vars", "toolhead", "heater_bed"):
            self.assertIn(k, s)

    def test_404(self):
        code, _ = rest_client(self.port, "GET", "/api/nope")
        self.assertEqual(code, 404)

    # --- job_start hard gate

    def _arm_block(self):
        self.fk.set_gate(bed_clear=False, ticks=1)
        self.fk.set_printing("printing")   # job alive when the worker queries

    def test_job_start_block_pauses(self):
        self._arm_block()
        self.g.apply_status({"print_stats": {"state": "printing",
                                             "print_duration": 0,
                                             "filename": "test.gcode"}})
        self.assertTrue(wait_for(lambda: any("PAUSE" in s for s in self.fk.scripts)))
        s = self.scripts_join()
        self.assertIn("PAUSE", s)
        self.assertIn("RESPOND TYPE=error", s)
        self.assertIn("M117", s)
        self.assertIsNotNone(self.g.pause_tracking)
        events = open(self.cfg.event_log).read()
        self.assertIn("gate_pause", events)

    def test_job_start_pause_timeout_polls_not_resends(self):
        # klippy busy -> PAUSE delivered but client times out. The daemon must
        # poll is_paused instead of re-sending (a second PAUSE double-parks
        # this machine and breaks RESUME restore — verified live).
        self._arm_block()
        self.fk.timeout_next_post = True
        self.g.apply_status({"print_stats": {"state": "printing",
                                             "print_duration": 0,
                                             "filename": "test.gcode"}})
        self.assertTrue(wait_for(lambda: self.g.pause_tracking is not None,
                                 timeout=20))
        time.sleep(0.5)
        pauses = [s for s in self.fk.scripts if s.strip() == "PAUSE"]
        self.assertEqual(len(pauses), 1)      # exactly one PAUSE sent
        self.assertTrue(self.g.pause_tracking)

    def test_job_start_override_passes_no_pause(self):
        self.fk.set_gate(bed_clear=False, ticks=1, override_active=True,
                         override_by="console_10min")
        self.fk.set_printing("printing")
        self.g.apply_status({"print_stats": {"state": "printing",
                                             "print_duration": 0,
                                             "filename": "t.gcode"}})
        time.sleep(self.cfg.pause_delay_s + 0.4)
        self.assertNotIn("PAUSE", self.scripts_join())
        self.assertIn("job_start_pass", open(self.cfg.event_log).read())

    def test_job_start_stale_passes_failopen(self):
        self.fk.set_gate(bed_clear=False, ticks=99)
        self.fk.set_printing("printing")
        self.g.apply_status({"print_stats": {"state": "printing",
                                             "print_duration": 0,
                                             "filename": "t.gcode"}})
        time.sleep(self.cfg.pause_delay_s + 0.4)
        self.assertNotIn("PAUSE", self.scripts_join())
        self.assertIn("stale_failopen", open(self.cfg.event_log).read())

    def test_job_start_kills_switch_passes(self):
        self.fk.set_gate(bed_clear=False, ticks=1, gate_enabled=False)
        self.fk.set_printing("printing")
        self.g.apply_status({"print_stats": {"state": "printing",
                                             "print_duration": 0,
                                             "filename": "t.gcode"}})
        time.sleep(self.cfg.pause_delay_s + 0.4)
        self.assertNotIn("PAUSE", self.scripts_join())

    def test_job_start_dead_job_skips(self):
        self._arm_block()
        # by the time the worker queries, the job is gone (state != printing)
        self.g.apply_status({"print_stats": {"state": "printing",
                                             "print_duration": 0,
                                             "filename": "t.gcode"}})
        self.fk.set_printing("standby")   # job died during the 2 s delay
        time.sleep(self.cfg.pause_delay_s + 0.6)
        self.assertNotIn("PAUSE", self.scripts_join())
        self.assertIn("job_not_active", open(self.cfg.event_log).read())

    def test_job_start_clear_bed_passes_silent(self):
        self.fk.set_gate(bed_clear=True, ticks=1)
        self.fk.set_printing("printing")
        self.g.apply_status({"print_stats": {"state": "printing",
                                             "print_duration": 0,
                                             "filename": "t.gcode"}})
        time.sleep(self.cfg.pause_delay_s + 0.4)
        self.assertNotIn("PAUSE", self.scripts_join())

    # --- escalation

    def test_escalation_after_threshold_then_hourly(self):
        self.g.print_stats = {"state": "paused"}
        self.fk.set_gate(bed_clear=False, ticks=1)
        self.fk.set_printing("paused")
        self.g.pause_tracking = {"job": "t.gcode", "since": time.time(),
                                 "escalations": 0}
        # before threshold: nothing
        self.g.escalation_sweep(time.time())
        self.assertNotIn("RESPOND", self.scripts_join())
        # past threshold: first escalation
        self.g.escalation_sweep(time.time() + self.cfg.escalate_after_s + 0.1)
        self.assertIn("RESPOND TYPE=error", self.scripts_join())
        self.assertEqual(self.g.pause_tracking["escalations"], 1)
        # before next repeat: no second
        self.g.escalation_sweep(time.time() + self.cfg.escalate_after_s + 0.2)
        self.assertEqual(self.g.pause_tracking["escalations"], 1)
        # after repeat interval: second
        self.g.escalation_sweep(time.time() + self.cfg.escalate_after_s +
                                self.cfg.escalate_repeat_s + 0.1)
        self.assertEqual(self.g.pause_tracking["escalations"], 2)

    # --- resume = logged override

    def test_resume_logged_as_override(self):
        self.g.pause_tracking = {"job": "t.gcode", "since": time.time(),
                                 "escalations": 0}
        self.g.apply_status({"pause_resume": {"is_paused": True}})
        self.g.apply_status({"pause_resume": {"is_paused": False}})
        events = open(self.cfg.event_log).read()
        self.assertIn("resume_override", events)
        self.assertIsNone(self.g.pause_tracking)

    # --- mid-print marking + job end

    def test_midprint_mark_after_120s(self):
        self.fk.set_gate(bed_clear=True, ticks=1)
        self.fk.set_printing("printing", duration=0.0)
        self.g.apply_status({"print_stats": {"state": "printing",
                                             "print_duration": 0,
                                             "filename": "t.gcode"}})
        self.g.apply_status({"print_stats": {"state": "printing",
                                             "print_duration": 150.0,
                                             "filename": "t.gcode"}})
        self.assertTrue(self.g.midprint_hold)
        self.assertIn("VARIABLE=bed_clear VALUE=False", self.scripts_join())

    def test_job_end_clears_hold_and_verdict(self):
        self.g.print_stats = {"state": "printing", "print_duration": 100.0,
                              "filename": "t.gcode"}
        self.g.verdict = {"occupied": False, "confidence": 0.9, "reason": "clear",
                          "ts": 0, "frame": None}
        self.g.verdict_received = time.time()
        self.g.midprint_hold = True
        self.g.apply_status({"print_stats": {"state": "complete",
                                             "print_duration": 900.0,
                                             "filename": "t.gcode"}})
        self.assertFalse(self.g.midprint_hold)
        self.assertIsNone(self.g.verdict)
        self.assertEqual(self.g.desired_bed_clear(time.time()), (True, "failopen"))

    # --- moonraker down: nothing destructive, no silent loss

    def test_moonraker_down_verdict_retained(self):
        rest_client(self.port, "POST", "/api/occupancy",
                    {"occupied": True, "confidence": 0.9, "reason": "blob",
                     "ts": time.time()})
        n0 = len(self.fk.scripts)
        self.fk.down = True
        self.g.heartbeat_once()          # fails, no crash
        code, resp = rest_client(self.port, "POST", "/api/occupancy",
                                 {"occupied": False, "confidence": 0.95,
                                  "reason": "empty", "ts": time.time()})
        self.assertEqual((code, resp["written"]), (200, False))
        self.fk.down = False
        self.g.heartbeat_once()          # retry writes retained verdict state
        self.assertIn("VARIABLE=bed_clear VALUE=True", self.scripts_join())
        self.assertGreater(len(self.fk.scripts), n0)

    # --- partial status updates (Moonraker sends changed fields only)

    def test_partial_print_stats_does_not_end_job(self):
        self.fk.set_gate(bed_clear=True, ticks=1)
        self.fk.set_printing("printing")
        self.g.apply_status({"print_stats": {"state": "printing",
                                             "print_duration": 0,
                                             "filename": "t.gcode"}})
        wait_for(lambda: os.path.exists(self.cfg.event_log))
        n_jobs = len([l for l in open(self.cfg.event_log).read().splitlines()
                      if '"job_start"' in l])
        # a partial update (no "state" key) must NOT be read as a job end
        self.g.apply_status({"print_stats": {"filament_used": 12.5}})
        self.assertEqual(self.g.print_stats["filament_used"], 12.5)
        self.assertEqual(self.g.print_stats["state"], "printing")
        events = open(self.cfg.event_log).read()
        self.assertNotIn("job_end", events)
        n_jobs2 = len([l for l in events.splitlines() if '"job_start"' in l])
        self.assertEqual(n_jobs2, n_jobs)

    def test_partial_pause_resume_does_not_log_override(self):
        open(self.cfg.event_log, "a").close()  # events file must exist
        # partial update without is_paused must not trigger any transition
        self.g.pause_tracking = {"job": "t.gcode", "since": time.time(),
                                 "escalations": 0}
        self.g.pause_resume = {"is_paused": True}
        self.g.apply_status({"pause_resume": {"filament_used": 1.0}})
        self.assertIsNotNone(self.g.pause_tracking)
        self.assertNotIn("resume_override", open(self.cfg.event_log).read())
        # resume with no tracked gate-pause -> not an override
        self.g.pause_tracking = None
        self.g.apply_status({"pause_resume": {"is_paused": False}})
        self.assertNotIn("resume_override", open(self.cfg.event_log).read())
        # tracked gate-pause + real transition -> logged override
        self.g.pause_tracking = {"job": "t.gcode", "since": time.time(),
                                 "escalations": 0}
        self.g.pause_resume = {"is_paused": True}
        self.g.apply_status({"pause_resume": {"is_paused": False}})
        self.assertIn("resume_override", open(self.cfg.event_log).read())

    def test_partial_gate_vars_merged_not_replaced(self):
        self.g.gate_vars = {"bed_clear": True, "ticks_since_update": 1,
                            "stale_ticks": 5, "override_active": False}
        self.g.apply_status({"gcode_macro BED_GATE": {"bed_clear": False}})
        gv = self.g.gate_vars
        self.assertEqual(gv["bed_clear"], False)
        self.assertEqual(gv["ticks_since_update"], 1)   # not lost to the partial
        self.assertEqual(gv["stale_ticks"], 5)

    def test_poll_query_format_reaches_fake(self):
        # poll_once uses Moon.query — the fake returns state only for the
        # correct (?name&name) format, so a passing poll proves the format
        self.fk.set_gate(bed_clear=False, ticks=2)
        self.fk.set_printing("printing")
        self.g.poll_once()
        self.assertEqual(self.g.gate_vars["bed_clear"], False)
        self.assertEqual(self.g.print_stats["state"], "printing")
        self.assertIsNotNone(self.g.toolhead) if "toolhead" in self.fk.state \
            else None

    def test_pause_transition_is_not_job_end(self):
        open(self.cfg.event_log, "a").close()  # events file must exist
        # printing -> paused must NOT clear the verdict/job/hold (live bug:
        # the gate paused a job and then released the gate on its own pause)
        self.g.verdict = {"occupied": True, "confidence": 0.9, "reason": "blob",
                          "ts": 0, "frame": None}
        self.g.verdict_received = time.time()
        self.g.print_stats = {"state": "printing", "print_duration": 50.0,
                              "filename": "t.gcode"}
        self.g.job = {"name": "t.gcode", "started_at": time.time(),
                      "override": False}
        self.g.pause_tracking = {"job": "t.gcode", "since": time.time(),
                                 "escalations": 0}
        self.g.apply_status({"print_stats": {"state": "paused",
                                             "print_duration": 50.0}})
        self.assertEqual(self.g.print_stats["state"], "paused")
        self.assertIsNotNone(self.g.verdict)          # verdict survives
        self.assertIsNotNone(self.g.pause_tracking)   # escalation still armed
        self.assertEqual(self.g.desired_bed_clear(time.time()), (False, "verdict"))
        # paused -> printing (human RESUME) must NOT re-fire the job gate
        n0 = len([l for l in open(self.cfg.event_log).read().splitlines()
                  if '"job_start"' in l])
        self.g.pause_resume = {"is_paused": True}
        self.g.apply_status({"pause_resume": {"is_paused": False}})
        self.g.apply_status({"print_stats": {"state": "printing",
                                             "print_duration": 55.0}})
        time.sleep(0.3)
        events = open(self.cfg.event_log).read()
        n1 = len([l for l in events.splitlines() if '"job_start"' in l])
        self.assertEqual(n1, n0)                       # no re-gate on resume
        self.assertIn("resume_override", events)       # logged as override
        self.assertTrue(self.g.job["override"])
        self.assertIsNone(self.g.pause_tracking)
        # cancel-while-paused also ends the job (clears verdict + hold)
        self.g.apply_status({"print_stats": {"state": "cancelled",
                                             "print_duration": 55.0}})
        self.assertIsNone(self.g.verdict)
        self.assertIn("job_end", open(self.cfg.event_log).read())

    # --- klippy restart: re-sync

    def test_klippy_reconnect_resync(self):
        rest_client(self.port, "POST", "/api/occupancy",
                    {"occupied": True, "confidence": 0.9, "reason": "blob",
                     "ts": time.time()})
        n0 = len(self.fk.scripts)
        self.g.on_klippy_connected()     # heartbeat_once + poll_once
        self.assertGreater(len(self.fk.scripts), n0)
        self.assertIn("VARIABLE=bed_clear VALUE=False", self.scripts_join())

    # --- heartbeat cadence

    def test_heartbeat_resets_ticks_every_beat(self):
        n0 = len(self.fk.scripts)
        self.g.heartbeat_once()
        self.g.heartbeat_once()
        sets = [s for s in self.fk.scripts[n0:]
                if s.startswith("SET_GCODE_VARIABLE")]
        self.assertGreaterEqual(len(sets), 2)
        for s in sets:
            self.assertIn("VARIABLE=ticks_since_update VALUE=0", s)
            self.assertIn("VARIABLE=bed_clear VALUE=", s)


class TestExecStop(unittest.TestCase):
    def test_exec_stop_reset_writes_fail_open(self):
        tmp = tempfile.mkdtemp()
        try:
            fk = FakeMoonServer()
            fk.set_gate(bed_clear=False, ticks=3)
            cfg = short_cfg(tmp)
            cfg.moonraker_http = fk.url()
            rc = bg.exec_stop_reset(cfg)
            self.assertEqual(rc, 0)
            s = "\n".join(fk.scripts)
            self.assertIn("VARIABLE=bed_clear VALUE=True", s)
            self.assertIn("VARIABLE=ticks_since_update VALUE=0", s)
            self.assertIn("RESPOND TYPE=command", s)
            fk.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
