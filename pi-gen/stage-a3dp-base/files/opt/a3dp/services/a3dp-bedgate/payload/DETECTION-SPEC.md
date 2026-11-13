# DETECTION-SPEC.md — bed-occupancy detection substrate & tuning (t_1ce4ee85, printer-owned)

## Substrate (exists, verified in t_cdf2aad7 / t_b9c23410 / t_e21cfbed)
- Rift camera: ustreamer, **localhost-only on the Rift**; full-res snapshots already
  pulled by a3dpmeter over SSH tunnel, 60 s timer.
- **Frames are rotate-180 flipped** — the deployed pipeline already compensates; any new
  consumer must too, or the bed ROI is mirrored.
- Dual detection already runs against this stream on the a3dpmeter lane (OP5 NPU
  yolo11-failure-fp16 @ ~123 ms/cycle + Pi 5 GLM check). Those models detect *print
  failures*, not pre-print occupancy — do not reuse them as the occupancy verdict.

## Method decision (printer's call): reference-diff, not YOLO
Bed-occupancy is "is the printable area empty" — a fixed-camera background-subtraction
problem, robust and cheap, unlike failure detection:
- Reference: bed-EMPTY frame captured under controlled lighting.
- Metric: per-pixel abs diff within the bed ROI → pixel count above per-pixel threshold.
- Occupied verdict: blob pixel count ≥ AREA_THRESH for N consecutive frames (N=2-3);
  clear verdict: ≤ CLEAR_THRESH for M consecutive frames (M=2). Hysteresis prevents
  flapping between CLEAR and OCCUPIED.
- Why not YOLO: no trained class for "arbitrary part on bed pre-print"; a diff model
  catches ANY object incl. ones never seen in training. Failure-model classes (spaghetti
  etc.) are a different problem on purpose.

## Conditions the gate must control (machine-side hooks exist)
- **Lighting:** PRINT_START already calls LIGHTS_ON; detection worker must request
  lights ON before capture (daemon: gcode `LIGHTS_ON`/`LIGHTS_OFF` or output_pin led)
  — otherwise occupancy verdicts depend on shop lighting.
- **Toolhead in frame:** if the parked/toolhead or gantry intersects the bed ROI in the
  image, the frame is INVALID — daemon knows toolhead position via Moonraker
  `toolhead.position` + axis maxima; skip evaluation when the head is inside the ROI's
  XY projection, and flag `reason:"toolhead_in_frame"`.
- **Bed temperature:** hot bed → convection shimmer + reflection shift. Capture
  empty-bed references BOTH at ambient and at print temps (≥3 temps: 25/60/110 °C);
  daemon reads `heater_bed.temperature` and picks the matching reference set, or widens
  the per-pixel threshold when |bed_temp − ref_temp| > 20 °C and marks lower confidence.
- **Evaluate only when idle:** no frames while printing/homing (motion blur + the part
  itself) except the deliberate mid-print 120 s marking per contract.

## Calibration / tuning procedure (data to collect on the Rift)
1. Empty-bed references: 20 frames × {lights on/off} × {bed 25/60/110 °C}. Store raw +
   compensated orientation.
2. Positive set: known part(s) on bed at ≥5 positions incl. edges/corners, small and
   large parts; 10 frames each.
3. Compute ROC of the diff metric (occupied vs empty) across lighting/temp combos;
   pick per-pixel threshold + AREA_THRESH so that false-NEGATIVE rate ≈ 0 at the cost
   of some false positives (warning-first: an extra warning is cheap, a crashed nozzle
   is not).
4. Re-tune triggers: camera moved/refocused, lighting changed, PEI sheet swapped,
   UI announces persistent false warnings.
5. Tuning artifacts land in print-monitoring repo `data/` (frames) + this spec updated
   with the measured thresholds — numbers, not vibes (same discipline as servo A/B).

## Initial thresholds (first-cut, to be replaced by measured values from step 3)
- per-pixel threshold: 18/255 gray levels
- AREA_THRESH (occupied): 0.3 % of bed-ROI pixels, 2 consecutive frames
- CLEAR_THRESH: 0.1 % of bed-ROI pixels, 2 consecutive frames
- confidence = min(1.0, blob_fraction / AREA_THRESH) clamped; verdicts below 0.5 →
  send reason "low_confidence" and daemon treats as no-verdict (stale path, fail open).
