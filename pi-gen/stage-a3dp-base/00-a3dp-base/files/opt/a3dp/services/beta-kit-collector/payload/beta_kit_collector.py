#!/opt/a3dp/detector-venv/bin/python
"""A3DP beta-kit collector — Phase-0 data collector for beta Modix kits.

Oneshot cycle, fired minute-aligned every 60 s by beta-kit-collector.timer:

  1. Snapshot from the kit's own ustreamer (:8080, crowsnest-managed).
  2. Scene guards: dark / obstructed frames are saved and marked
     `nondiagnostic` (no burst, no inference).
  3. YOLO verdict (ONNX CPU, /255-normalized NCHW — the RKNN path's raw
     0-255 NHWC preprocessing saturates confidences, do NOT port it here)
     -> verdicts.jsonl. VERDICTS ARE NEVER PRUNED: they are the durable
     record once frames evict.
  4. Alarm-class verdict -> incident burst: 10 s cadence capture loop in
     this same oneshot until the verdict clears or burst_max_minutes.
     Burst frames go to frames/burst/ so cache eviction spares them.
  5. manifest.jsonl: per-frame sha256 (the pull-side transport verifies
     against this).
  6. Cache cap (default 8 GB) over the frames tree; eviction oldest-
     regular-first, bursts spared.

Optional read-only printer link: if [printer] collect_state=true and the
tester's Moonraker answers, print_stats is GET-ed (single-object query —
the fleet Moonraker quirk) and idle minutes capture nothing. Unreachable
or disabled -> state=unknown, policy downgrades to every Nth frame.
The kit NEVER writes to the printer: no POST, no PUT, no gcode, no
listener. Job filenames are not collected.

Design: print-monitoring/docs/beta-data-kit.md §5-§6.
"""
import argparse
import configparser
import fcntl
import hashlib
import json
import logging
import logging.handlers
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.ini"

CLASSES = {0: "spaghetti", 1: "stringing", 2: "zits"}

SCENE_DARK = "nondiagnostic-dark"
SCENE_OBSTRUCTED = "nondiagnostic-obstructed"


def log_event(log, level, msg, **kw):
    if kw:
        log.log(level, "%s %s", msg, json.dumps(kw, default=str))
    else:
        log.log(level, msg)


def http_get(url, timeout):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def moonraker_print_state(base_url, timeout=10):
    """GET print_stats only (single-object query — fleet Moonraker quirk).
    Read-only: this is the ONLY printer-facing call in the kit stack."""
    res = json.loads(http_get(
        f"{base_url}/printer/objects/query?print_stats", timeout).decode())
    ps = res["result"]["status"]["print_stats"]
    return ps.get("state", "unknown"), ps.get("filename", "")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def rss_mb():
    for line in open("/proc/self/status"):
        if line.startswith("VmRSS:"):
            return round(int(line.split()[1]) / 1024, 1)
    return None


def scene_guard(img, dark_luma, uniform_std):
    """Dark-frame / obstructed self-report. Returns scene tag or None."""
    import cv2
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if float(gray.mean()) < dark_luma:
        return SCENE_DARK
    if float(gray.std()) < uniform_std:
        return SCENE_OBSTRUCTED
    return None


def onnx_detect(model_path, frame_path, conf_thres):
    """Load-and-run the ONNX failure model on one frame via onnxruntime CPU.
    Returns (detections, load_s, infer_s). Session init per cycle keeps the
    oneshot stateless; load time is reported separately from inference.

    Preprocessing: /255-normalized NCHW (verified 26/26 parity with the
    deployed fp16 RKNN, print-monitoring/docs/pi5-onbox-detection.md)."""
    import cv2
    import onnxruntime as ort
    img = cv2.imread(str(frame_path))
    if img is None:
        raise RuntimeError("cv2.imread failed")
    x = cv2.resize(img, (640, 640), interpolation=cv2.INTER_LINEAR)
    x = cv2.cvtColor(x, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    x = np.transpose(x, (2, 0, 1))[np.newaxis]  # NHWC -> NCHW
    t0 = time.perf_counter()
    sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
    inp_name = sess.get_inputs()[0].name
    load_s = round(time.perf_counter() - t0, 3)
    t1 = time.perf_counter()
    outs = sess.run(None, {inp_name: x})
    infer_s = round(time.perf_counter() - t1, 3)
    a = np.asarray(outs[0])[0]
    if a.ndim != 2 or a.shape[0] < 5:
        raise RuntimeError(f"unexpected output layout {a.shape}")
    cls = a[4:, :]
    cls_ids = cls.argmax(axis=0)
    confs = cls.max(axis=0)
    keep = np.where(confs > conf_thres)[0]
    order = keep[np.argsort(-confs[keep])][:10]
    dets = [(int(cls_ids[i]), float(confs[i]),
             [float(v) for v in a[:4, i]]) for i in order]
    return dets, load_s, infer_s


def make_verdict(dets, conf_thres):
    if dets:
        best = max(dets, key=lambda d: d[1])
        return {
            "state": CLASSES.get(best[0], f"cls{best[0]}"),
            "confidence": round(best[1], 3),
            "detections": [{"cls": CLASSES.get(c, f"cls{c}"),
                            "conf": round(s, 3),
                            "box_xyxy_640": [round(v) for v in b]}
                           for c, s, b in dets],
        }
    return {"state": "ok", "confidence": 0.0, "detections": []}


class Store:
    """Frame cache + verdict/manifest writers under data_dir.

    Layout:
      <data>/frames/YYYY-MM-DD/HHMMSS.jpg      regular minute frames
      <data>/frames/burst/<id>/HHMMSS.jpg      incident bursts (eviction-spared)
      <data>/verdicts.jsonl                    never pruned
      <data>/manifest.jsonl                    per-frame sha256, transport index
      <data>/state.json                        collector bookkeeping
    """

    def __init__(self, data_dir, cap_bytes, log):
        self.root = Path(data_dir)
        self.frames = self.root / "frames"
        self.cap = cap_bytes
        self.log = log
        (self.root / "logs").mkdir(parents=True, exist_ok=True)
        self.frames.mkdir(parents=True, exist_ok=True)

    def _write_jsonl(self, name, entry):
        with (self.root / name).open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def capture(self, data, burst_id=None):
        """Write one frame; returns (relpath, sha, bytes)."""
        now = datetime.now(timezone.utc)
        day = now.strftime("%Y-%m-%d")
        name = now.strftime("%H%M%S") + ".jpg"
        if burst_id:
            d = self.frames / "burst" / burst_id / day
        else:
            d = self.frames / day
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        # sub-second collision (burst cadence can land two frames in one
        # wall-clock second after a slow cycle): never silently overwrite
        n = 0
        while p.exists():
            n += 1
            p = d / (now.strftime("%H%M%S")
                     + f"-{now.microsecond // 1000:03d}-{n:02d}.jpg")
        p.write_bytes(data)
        rel = str(p.relative_to(self.root))
        return rel, sha256_bytes(data), len(data)

    def record(self, rel, sha, nbytes, verdict, scene, pstate, burst_id,
               latency=None):
        """One verdicts.jsonl row + matching manifest.jsonl row."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "frame": rel,
            "frame_sha256": sha,
            "bytes": nbytes,
            "mode": "onnx-cpu",
            "model": str(verdict.get("model", "")),
            "scene": scene or "ok",
            "printer_state": pstate,
            "burst": burst_id,
            "latency": latency or {},
            "rss_mb": rss_mb(),
            "verdict": {k: v for k, v in verdict.items() if k != "model"},
        }
        self._write_jsonl("verdicts.jsonl", entry)
        self._write_jsonl("manifest.jsonl", {
            "ts": entry["ts"],
            "frame": rel,
            "sha256": sha,
            "bytes": nbytes,
            "kind": "burst" if burst_id else "frame",
            "verdict_state": verdict["state"],
        })
        log_event(self.log, logging.INFO, "verdict recorded", frame=rel,
                  state=verdict["state"], scene=entry["scene"], burst=burst_id)

    def usage_bytes(self):
        total = 0
        for dirpath, _dirs, files in os.walk(self.frames):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        return total

    def evict(self, active_burst_id=None):
        """Cache cap enforcement. Oldest regular frames first; burst frames
        spared until regulars alone cannot free enough, and never evicted
        from the currently-open burst."""
        usage = self.usage_bytes()
        if usage <= self.cap:
            return
        entries = []
        for p in self.frames.rglob("*.jpg"):
            try:
                st = p.stat()
                entries.append((st.st_mtime, str(p),
                                "burst" in p.relative_to(self.frames).parts,
                                st.st_size))
            except OSError:
                continue
        regulars = sorted(e for e in entries if not e[2])
        bursts = sorted(e for e in entries if e[2])
        freed = 0

        def rm(e):
            nonlocal freed
            try:
                os.remove(e[1])
                freed += e[3]
            except OSError:
                pass

        for e in regulars:
            if usage - freed <= self.cap:
                break
            rm(e)
        for e in bursts:
            if usage - freed <= self.cap:
                break
            if active_burst_id and active_burst_id in e[1]:
                continue
            rm(e)
        if freed:
            log_event(self.log, logging.INFO, "cache eviction",
                      freed_bytes=freed)
        # prune now-empty regular day dirs (burst dirs keep their identity)
        for d in sorted(self.frames.iterdir()):
            if d.is_dir() and d.name != "burst" and not any(d.iterdir()):
                d.rmdir()


def load_state(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def save_state(path, st):
    Path(path).write_text(json.dumps(st, indent=1, sort_keys=True))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = ap.parse_args()

    cfg = configparser.ConfigParser(inline_comment_prefixes=("#",))
    if not cfg.read(args.config):
        print(f"ERROR: config not readable: {args.config}", file=sys.stderr)
        return 1
    cam_url = cfg["camera"]["snapshot_url"]
    rotate_180 = cfg.getboolean("camera", "preprocess_rotate_180", fallback=False)
    model_path = cfg["model"]["model_path"]
    conf_thres = cfg.getfloat("model", "conf_threshold", fallback=0.30)
    model_name = Path(model_path).name
    alarm_classes = {s.strip() for s in cfg.get(
        "model", "alarm_classes", fallback="").split(",") if s.strip()}

    data_dir = cfg.get("collect", "data_dir", fallback="/home/pi/kit-data")
    every_nth = cfg.getint("collect", "collect_every_nth", fallback=5)
    burst_cadence = cfg.getint("collect", "burst_cadence_s", fallback=10)
    burst_max_s = cfg.getint("collect", "burst_max_minutes", fallback=30) * 60
    cap_bytes = cfg.getint("collect", "cache_cap_gb", fallback=8) * (1 << 30)
    dark_luma = cfg.getfloat("collect", "dark_luma_threshold", fallback=12.0)
    uniform_std = cfg.getfloat("collect", "uniform_std_threshold", fallback=3.0)

    collect_state = cfg.getboolean("printer", "collect_state", fallback=True)
    mr_url = cfg.get("printer", "moonraker_url", fallback="").strip()

    store = Store(data_dir, cap_bytes, logging.getLogger("kit-collector"))
    log = logging.getLogger("kit-collector")
    if not log.handlers:
        rh = logging.handlers.RotatingFileHandler(
            store.root / "logs" / "collector.log",
            maxBytes=5_000_000, backupCount=3)
        rh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(rh)
        log.setLevel(logging.INFO)

    # non-blocking instance lock: a held lock = burst still running, skip
    lock_fh = None
    for lock_path in (f"/run/beta-kit-collector/collector.lock",
                      str(store.root / "collector.lock")):
        try:
            lock_fh = open(lock_path, "w")
            break
        except OSError:
            continue
    if lock_fh is not None:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log_event(log, logging.INFO, "another cycle running; skipping")
            return 0

    st = load_state(store.root / "state.json")

    # ---- printer state (read-only, optional) --------------------------------
    pstate = "unknown"
    if collect_state and mr_url:
        try:
            pstate, job = moonraker_print_state(mr_url)
            st["last_moonraker_error"] = None
        except Exception as e:
            st["last_moonraker_error"] = str(e)[:200]
            log_event(log, logging.WARNING, "moonraker unreachable "
                      "(state=unknown, downgrade policy)", error=str(e)[:200])
    if collect_state and not mr_url:
        st["last_moonraker_error"] = "collect_state=true but moonraker_url empty"

    # idle gate: known state + not printing -> no frame this minute
    if pstate not in ("unknown",) and pstate != "printing":
        st["printer_state"] = pstate
        st["last_cycle_ts"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        save_state(store.root / "state.json", st)
        store.evict()
        return 0
    # unknown-state downgrade policy: keep every Nth minute frame
    minute = int(time.time()) // 60
    if pstate == "unknown" and every_nth > 1 and minute % every_nth != 0:
        st["printer_state"] = pstate
        st["last_cycle_ts"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        save_state(store.root / "state.json", st)
        store.evict()
        return 0

    # ---- snapshot ------------------------------------------------------------
    try:
        data = http_get(cam_url, 30)
        if not data or len(data) < 1000:
            raise RuntimeError(f"snapshot too small ({len(data)} B)")
    except Exception as e:
        log_event(log, logging.WARNING, "snapshot failed", error=str(e)[:200])
        st["last_snapshot_error"] = str(e)[:200]
        st["last_cycle_ts"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        save_state(store.root / "state.json", st)
        return 0
    st["last_snapshot_error"] = None

    import cv2
    tmp_path = store.root / "logs" / "incoming.jpg"
    tmp_path.write_bytes(data)
    img = cv2.imread(str(tmp_path))
    if img is None:
        log_event(log, logging.WARNING, "frame decode failed; frame dropped")
        st["last_cycle_ts"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        save_state(store.root / "state.json", st)
        return 0
    if rotate_180:
        img = cv2.rotate(img, cv2.ROTATE_180)
        ok, buf = cv2.imencode(".jpg", img)
        if ok:
            data = buf.tobytes()
            tmp_path.write_bytes(data)

    scene = scene_guard(img, dark_luma, uniform_std)
    rel, sha, nbytes = store.capture(data)
    if scene:
        # frame saved, verdict marked nondiagnostic, no inference, no burst
        store.record(rel, sha, nbytes,
                     verdict={"state": "nondiagnostic", "confidence": 0.0,
                              "detections": [], "model": model_name},
                     scene=scene, pstate=pstate, burst_id=None)
        st["printer_state"] = pstate
        st["last_cycle_ts"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        save_state(store.root / "state.json", st)
        store.evict()
        return 0

    # ---- verdict -------------------------------------------------------------
    def infer_and_record(rel, sha, nbytes, burst_id=None):
        dets, load_s, infer_s = onnx_detect(model_path, store.root / rel,
                                            conf_thres)
        verdict = make_verdict(dets, conf_thres)
        verdict["model"] = model_name
        store.record(rel, sha, nbytes, verdict, None, pstate, burst_id,
                     latency={"load_s": load_s, "infer_s": infer_s})
        return verdict

    try:
        verdict = infer_and_record(rel, sha, nbytes)
    except Exception as e:
        st["consecutive_fail"] = st.get("consecutive_fail", 0) + 1
        log_event(log, logging.WARNING, "onnx cycle failed",
                  fail_count=st["consecutive_fail"], error=str(e)[:250])
        st["printer_state"] = pstate
        st["last_cycle_ts"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        save_state(store.root / "state.json", st)
        return 0
    st["consecutive_fail"] = 0

    is_alarm = (verdict["state"] != "ok"
                and (not alarm_classes or verdict["state"] in alarm_classes))

    # ---- incident burst (in-oneshot loop; systemd merges timer ticks) -------
    if is_alarm:
        burst_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        log_event(log, logging.INFO, "incident opened — burst capture",
                  state=verdict["state"], burst=burst_id)
        st["burst_active"] = burst_id
        t_start = time.time()
        while True:
            time.sleep(burst_cadence)
            try:
                bdata = http_get(cam_url, 30)
                if not bdata or len(bdata) < 1000:
                    raise RuntimeError(f"snapshot too small ({len(bdata)} B)")
                bimg = cv2.imdecode(np.frombuffer(bdata, np.uint8),
                                    cv2.IMREAD_COLOR)
                if bimg is None:
                    raise RuntimeError("frame decode failed")
                if rotate_180:
                    bimg = cv2.rotate(bimg, cv2.ROTATE_180)
                    ok, buf = cv2.imencode(".jpg", bimg)
                    if ok:
                        bdata = buf.tobytes()
                if scene_guard(bimg, dark_luma, uniform_std):
                    continue  # dark/obstructed burst frame: skip, keep bursting
                rel, sha, nbytes = store.capture(bdata, burst_id)
                verdict = infer_and_record(rel, sha, nbytes, burst_id)
            except Exception as e:
                log_event(log, logging.WARNING, "burst cycle failed",
                          burst=burst_id, error=str(e)[:200])
                verdict = {"state": "unknown", "confidence": 0.0}
            clear = (verdict["state"] == "ok"
                     or (alarm_classes and verdict["state"] not in alarm_classes))
            if clear:
                log_event(log, logging.INFO, "incident closed (verdict clear)",
                          burst=burst_id)
                break
            if time.time() - t_start > burst_max_s:
                log_event(log, logging.WARNING,
                          "burst cap reached — incident left open", burst=burst_id)
                break
        st["burst_active"] = None

    st["printer_state"] = pstate
    st["last_cycle_ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_state(store.root / "state.json", st)
    store.evict()
    return 0


if __name__ == "__main__":
    sys.exit(main())
