#!/usr/bin/env python3
"""Payload assurance: the BAKED :8082 flasher payload must emit valid JS.

The image repo does not carry the flasher's full unit suite (that lives in
printer-hardware tests/test_mcu_flash.py); this test pins the one class of
defect that shipped before and is invisible to a Python-side parse: the PAGE
template emitting literal newlines into JS single-quoted strings, which kills
the entire inline <script> (board table rendered header-only).

Regression (t_08814a92 / t_6da11747): compiles the ACTUAL inline <script>
emitted by the baked files/usr/local/bin/a3dp-mcu-flash in node (real JS
engine) and drives pollFull through a DOM/fetch harness to assert multi-line
status output. It also asserts the baked payload carries the full-update
surface (`full` subcommand + /api/full endpoints) so the :8082 UI matches the
canonical printer-hardware flasher, not a stale pre-full-update copy.

Run: python3 tests/test_baked_flasher_payload.py
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAYLOAD = os.path.join(REPO, "pi-gen", "stage-a3dp-base", "00-a3dp-base",
                       "files", "usr", "local", "bin", "a3dp-mcu-flash")

_loader = SourceFileLoader("a3dp_mcu_flash_baked", PAYLOAD)
spec = importlib.util.spec_from_loader("a3dp_mcu_flash_baked", _loader)
assert spec is not None and spec.loader is not None, "cannot load baked payload"
mf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mf)


class TestBakedPayloadJavaScript(unittest.TestCase):
    """The baked flasher's inline <script> must be valid JavaScript in a
    real JS engine, and pollFull must render multi-line status."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("node") is None:
            raise unittest.SkipTest("node (real JS engine) not available")
        m = re.search(r"<script>(.*)</script>", mf.PAGE, re.S)
        assert m, "baked PAGE template has no inline <script> block"
        cls.page_js = m.group(1)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.js_path = os.path.join(cls.tmp.name, "page_inline.js")
        with open(cls.js_path, "w") as f:
            f.write(cls.page_js)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _node(self, js_code):
        return subprocess.run(["node", "-e", js_code, self.js_path],
                              capture_output=True, text=True, timeout=60)

    def test_inline_script_compiles_in_real_js_engine(self):
        # Mirrors the on-page repro: new Function(document.scripts[0]...)
        r = self._node(
            "const src = require('fs').readFileSync(process.argv[1],'utf8');"
            "try { new Function(src); console.log('JS_OK'); }"
            "catch (e) { console.log('JS_ERR: ' + e.message); process.exit(1); }")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("JS_OK", r.stdout)

    def test_pollfull_output_keeps_newlines(self):
        # Behavior check: pollFull renders multi-line status (steps joined
        # with newlines, error on its own line) — not one collapsed blob.
        harness = r'''
const fs = require('fs');
const realSetTimeout = setTimeout;
const src = fs.readFileSync(process.argv[1], 'utf8');
const els = {};
const el = (id) => els[id] || (els[id] = {textContent:'', innerHTML:'',
  style:{}, rows:[{outerHTML:''}], checked:false, value:'', appendChild(){}});
global.document = { getElementById: el };
global.confirm = () => true;
global.setInterval = () => 0;
global.setTimeout = () => 0;
global.fetch = async (url) => {
  const body =
    url === '/api/boards' ? {serial:[], bootsel:[], firmware:[],
        firmware_dir:'/fw', versions:{}, klipper_running:false} :
    url === '/api/full/plan' ? {guard:{ok:true, reason:'r'},
        host_version:'v', slots:[]} :
    url === '/api/full/start' ? {ok:true, msg:'started', slots:['s1']} :
    {running:false, started:'T0',
     steps:[{t:'10:00', msg:'step one'}], error:'boom'};
  return { json: async () => body };
};
(0, eval)(src);
el('fc').checked = true;
doFull().then(() => realSetTimeout(() => {
  console.log(JSON.stringify(
    {flog: els['flog'] ? els['flog'].textContent : null}));
}, 50));
'''
        r = self._node(harness)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        flog = out["flog"]
        self.assertIsNotNone(flog, "pollFull never wrote to #flog")
        # note: doFull's 'STARTED' message is overwritten by pollFull's
        # status write to the same #flog element (existing page behavior)
        self.assertIn("DONE since T0", flog)
        self.assertIn("10:00  step one", flog)
        self.assertIn("ERROR: boom", flog)
        self.assertIn("\n", flog)          # real newlines between lines
        self.assertNotIn("\r", flog)


class TestBakedPayloadIsCurrentFlasher(unittest.TestCase):
    """The baked payload must BE the current canonical flasher (with the
    full-update surface and the fixed pollFull), not a stale pre-full-update
    copy — the image is the fleet's source of the :8082 recovery flasher."""

    def _src(self):
        with open(PAYLOAD, encoding="utf-8") as f:
            return f.read()

    def test_payload_carries_full_update_surface(self):
        src = self._src()
        # short custom messages: never let a failure dump the 68 KB payload
        self.assertTrue("sub.add_parser(\"full\"" in src,
                        "no `full` subcommand in baked payload")
        self.assertTrue("/api/full/plan" in src)
        self.assertTrue("/api/full/status" in src)

    def test_pollfull_strings_are_js_escaped(self):
        # The three pollFull literals must use JS \n escapes in the SOURCE
        # template; a literal newline inside a single-quoted JS string is a
        # SyntaxError (the original defect).
        m = re.search(r"async function pollFull\(\)\{.*?\n\}", mf.PAGE, re.S)
        assert m, "pollFull not found in baked PAGE template"
        body = m.group(0)
        self.assertTrue("\\n" in body, "pollFull has no JS \\n escapes")
        # every emitted single-quoted JS string inside pollFull must sit on
        # one physical line of the template (no raw newlines inside quotes):
        # a quote-open ... newline ... quote-close within one line pair means
        # the emitted JS is a SyntaxError.
        opened = None
        for i, line in enumerate(body.splitlines()):
            for ch_i, ch in enumerate(line):
                if ch == "'" and (ch_i == 0 or line[ch_i - 1] != "\\"):
                    opened = None if opened is not None else (i, ch_i)
            # any quote still open at end-of-line inside pollFull = defect
            self.assertIsNone(
                opened,
                f"unterminated JS string at pollFull template line {i}")

    def test_baked_matches_canonical_source_version(self):
        # The baked payload must expose the same protocol version markers as
        # the canonical printer-hardware flasher: verify + full subcommands
        # and the BY_ID_RE serial-tail fix (deterministic A3DP serials).
        src = self._src()
        self.assertTrue("sub.add_parser(\"verify\"" in src,
                        "baked payload lacks the `verify` subcommand")
        self.assertTrue("usb-.*_(.+)-if00" in src,
                        "baked payload lacks the generic by-id serial-tail regex")


if __name__ == "__main__":
    unittest.main(verbosity=2)
