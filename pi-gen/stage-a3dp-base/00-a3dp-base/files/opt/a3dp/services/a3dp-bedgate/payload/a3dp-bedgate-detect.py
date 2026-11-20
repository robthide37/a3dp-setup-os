#!/usr/bin/env python3
"""a3dp-bedgate-detect — ON-MACHINE bed-occupancy detection worker.

Implements configs/bed-gate/DETECTION-SPEC.md (printer-owned): fixed-camera
reference-diff occupancy detection. Runs on the printer's own Pi (Rob's
on-board experiment, t_39bbe296), pulls frames from the crowsnest/ustreamer
snapshot endpoint, decodes via ffmpeg to fixed-size raw grayscale (no PIL
dependency — klippy-env numpy is the only non-stdlib import), diffs against
the empty-bed reference inside the bed ROI, applies hysteresis, and POSTs
verdicts to the a3dp-bedgate daemon (POST /api/occupancy, nginx :8083).

SAFETY (GATE-CONTRACT: never write bed_clear on a guess):
  POSTING IS OFF BY DEFAULT (A3DP_BEDGATE_DETECT_POST=0 = dry-run). Run
  dry-run whenever the camera does not actually see the bed, the ROI is
  uncalibrated, or the empty-bed references are not captured per spec
  (lights on, >=3 bed temps). In dry-run the worker still computes and logs
  every metric — it exists to (a) measure CPU/mem/compute cost on this Pi
  and (b) produce the data needed for threshold calibration.

Frame chain (DETECTION-SPEC): ustreamer snapshot -> ffmpeg scale 640x480 ->
rotate-180 flip if configured (Rift's stream is flipped; set per machine) ->
crop ROI -> absdiff vs reference -> fraction of pixels > per-pixel threshold
-> hysteresis (N consecutive occupied / M consecutive clear) -> verdict.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional, TypeVar

T = TypeVar("T")

import numpy as np

W, H = 640, 480  # everything is normalized to this before ROI math


def env(name: str, default: T, cast: Optional[Callable[[str], T]] = None) -> T:
    v = os.environ.get(name)
    if v in (None, ""):
        return default
    return cast(v) if cast is not None else default


def log(msg):
    print(f"[bedgate-detect {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Cfg:
    def __init__(self):
        self.snapshot_url = env("A3DP_DETECT_SNAPSHOT_URL",
                                "http://127.0.0.1:8080/?action=snapshot")
        self.interval_s = env("A3DP_DETECT_INTERVAL_S", 5.0, float)
        self.roi = env("A3DP_DETECT_ROI", "")              # "x,y,w,h" or "" = full frame
        self.pixel_thresh = env("A3DP_DETECT_PIXEL_THRESH", 18, int)   # /255 gray
        self.area_occ = env("A3DP_DETECT_AREA_OCC", 0.003, float)      # 0.3 % of ROI
        self.area_clear = env("A3DP_DETECT_AREA_CLEAR", 0.001, float)  # 0.1 %
        self.consec_occ = env("A3DP_DETECT_CONSEC_OCC", 2, int)
        self.consec_clear = env("A3DP_DETECT_CONSEC_CLEAR", 2, int)
        self.post_enabled = env("A3DP_DETECT_POST", 0, int) == 1
        self.daemon_url = env("A3DP_DETECT_DAEMON_URL",
                              "http://127.0.0.1:8083")
        self.moonraker = env("A3DP_DETECT_MOONRAKER",
                             "http://127.0.0.1:7125")
        self.flip = env("A3DP_DETECT_FLIP", "none")        # none | rot180
        self.idle_only = env("A3DP_DETECT_IDLE_ONLY", 1, int) == 1
        self.ref_frames = env("A3DP_DETECT_REF_FRAMES", 5, int)
        self.ref_path = os.path.expanduser(
            env("A3DP_DETECT_REF_PATH", "~/a3dp-bedgate/detect-reference.npy"))
        self.log_path = os.path.expanduser(env(
            "A3DP_DETECT_LOG",
            "~/printer_data/logs/a3dp-bedgate-detect.jsonl"))
        assert self.flip in ("none", "rot180"), "flip must be none|rot180"


def parse_roi(roi):
    if not roi:
        return 0, 0, W, H
    x, y, w, h = (int(p) for p in roi.split(","))
    assert 0 <= x and 0 <= y and x + w <= W and y + h <= H, f"bad ROI {roi}"
    return x, y, w, h


def fetch_jpeg(url, timeout=6, retries=3):
    """ustreamer occasionally returns an empty/short body under load —
    retry and validate the JPEG magic before handing it to ffmpeg."""
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as f:
                data = f.read()
            if len(data) > 1000 and data[:2] == b"\xff\xd8":
                return data
            last = RuntimeError(f"bad snapshot body: len={len(data)}")
        except Exception as e:
            last = e
        time.sleep(1.0)
    raise last


def decode_gray(jpeg):
    """JPEG -> (H,W) uint8 via ffmpeg rawvideo (no PIL on the Pi)."""
    p = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", "pipe:0",
         "-vf", f"scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "gray",
         "pipe:1"],
        input=jpeg, capture_output=True, timeout=15)
    if p.returncode != 0 or len(p.stdout) != W * H:
        raise RuntimeError(f"ffmpeg decode failed rc={p.returncode} "
                           f"len={len(p.stdout)} err={p.stderr[:200]}")
    return np.frombuffer(p.stdout, dtype=np.uint8).reshape(H, W)


def rss_mb():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    return -1


def moon_state(base):
    """print_stats.state for the idle-only rule; None if unreachable."""
    try:
        with urllib.request.urlopen(base + "/printer/objects/query?print_stats",
                                    timeout=4) as f:
            d = json.load(f)
        return d["result"]["status"]["print_stats"]["state"]
    except Exception:
        return None


def post_verdict(daemon_url, occupied, confidence, reason, frame_path=None):
    body = json.dumps({"occupied": occupied, "confidence": round(confidence, 3),
                       "reason": reason, "ts": time.time(),
                       "frame": frame_path}).encode()
    req = urllib.request.Request(daemon_url + "/api/occupancy", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=8) as f:
        return f.status, f.read().decode()


class Jsonl:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def add(self, **kw):
        kw["ts"] = round(time.time(), 3)
        with self.lock:
            try:
                with open(self.path, "a") as f:
                    f.write(json.dumps(kw, default=str) + "\n")
            except OSError as e:
                log(f"log write failed: {e}")


def capture_reference(cfg, jl):
    """Median of N frames as the empty-bed reference (spec step 1; ONLY valid
    when the camera actually sees an EMPTY bed — dry-run mode tolerates an
    arbitrary scene, real mode must be re-captured after camera moves)."""
    frames = []
    for i in range(cfg.ref_frames):
        ref = decode_gray(fetch_jpeg(cfg.snapshot_url))
        if cfg.flip == "rot180":
            ref = ref[::-1, ::-1]
        frames.append(ref.astype(np.int16))
        if i < cfg.ref_frames - 1:
            time.sleep(1.0)
    ref = np.median(np.stack(frames), axis=0).astype(np.uint8)
    os.makedirs(os.path.dirname(cfg.ref_path), exist_ok=True)
    np.save(cfg.ref_path, ref)
    jl.add(type="reference_captured", path=cfg.ref_path,
           frames=cfg.ref_frames, post_enabled=cfg.post_enabled)
    log(f"reference captured: {cfg.ref_path} ({cfg.ref_frames} frames)")
    return ref


def main():
    cfg = Cfg()
    roi = parse_roi(cfg.roi)
    jl = Jsonl(cfg.log_path)
    log(f"start: url={cfg.snapshot_url} roi={cfg.roi or 'full'} interval={cfg.interval_s}s "
        f"flip={cfg.flip} POST={'ON' if cfg.post_enabled else 'OFF (dry-run)'}")

    if os.path.exists(cfg.ref_path):
        ref = np.load(cfg.ref_path)
        log(f"loaded existing reference {cfg.ref_path}")
    else:
        ref = capture_reference(cfg, jl)

    occ_streak = clear_streak = 0
    cycle = 0
    while True:
        t0 = time.monotonic()
        entry = {"type": "cycle", "n": cycle, "post_enabled": cfg.post_enabled}
        try:
            jpeg = fetch_jpeg(cfg.snapshot_url)
            t1 = time.monotonic()
            frame = decode_gray(jpeg)
            t2 = time.monotonic()
            if cfg.flip == "rot180":
                frame = frame[::-1, ::-1]
            x, y, w, h = roi
            f = frame[y:y + h, x:x + w].astype(np.int16)
            r = ref[y:y + h, x:x + w].astype(np.int16)
            diff = np.abs(f - r)
            blob_frac = float((diff > cfg.pixel_thresh).mean())
            mean_diff = float(diff.mean())
            state = moon_state(cfg.moonraker)
            evaluated = True
            reason = "ok"
            if cfg.idle_only and state not in (None, "standby", "complete",
                                               "cancelled", "error"):
                evaluated = False
                reason = f"not_idle:{state}"
            verdict = None
            if evaluated:
                if blob_frac >= cfg.area_occ:
                    occ_streak += 1
                    clear_streak = 0
                elif blob_frac <= cfg.area_clear:
                    clear_streak += 1
                    occ_streak = 0
                else:
                    occ_streak = clear_streak = 0  # hysteresis dead band
                if occ_streak >= cfg.consec_occ:
                    verdict = True
                elif clear_streak >= cfg.consec_clear:
                    verdict = False
            entry.update(fetch_ms=round((t1 - t0) * 1000, 1),
                         decode_ms=round((t2 - t1) * 1000, 1),
                         total_ms=round((time.monotonic() - t0) * 1000, 1),
                         mean_diff=round(mean_diff, 2),
                         blob_frac=round(blob_frac, 5),
                         state=state, evaluated=evaluated, reason=reason,
                         verdict=verdict,
                         occ_streak=occ_streak, clear_streak=clear_streak,
                         rss_mb=rss_mb())
            posted = None
            if cfg.post_enabled and verdict and evaluated:
                conf = min(1.0, blob_frac / cfg.area_occ)
                if conf >= 0.5:
                    code, resp = post_verdict(
                        cfg.daemon_url, verdict, conf,
                        "reference_diff" if verdict else "reference_diff_clear")
                    posted = (code, resp[:120])
                else:
                    posted = "skipped_low_confidence"
            if posted is not None:
                entry["posted"] = posted
        except Exception as e:
            entry.update(error=f"{type(e).__name__}: {e}"[:200])
            log(f"cycle error: {entry['error']}")
        jl.add(**entry)
        cycle += 1
        time.sleep(max(0.2, cfg.interval_s - (time.monotonic() - t0)))


if __name__ == "__main__":
    sys.exit(main())
