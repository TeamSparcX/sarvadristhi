# Validation report (SIH26150 deliverable)

Sarvadristhi 0.1.0 on dvr-pramanshala 0.4.0 · October 2026

## Summary

On test evidence with frame-level ground truth, native recovery found every surviving frame
on all seven disk images, with no false positives, correct camera, and recorder time exact
to within 1 s. The vendor-agnostic path, which ignores vendor parsers entirely, also
recovered every surviving frame and attributed 98.9–99.8% of them to the right camera from
the burned-in overlay; on footage with no overlay it separated three cameras filming an
identical scene by sensor fingerprint alone, and correctly refused to attribute when no
fingerprint was present.

**These results are on synthetic pictures in vendor-exact containers. They are not results on
real recorders.** Real-drive validation (section 6) is required before any operational claim.

## 1. Test evidence

`pramanshala` renders three cameras (H.264 Main, 640×360, 10 fps, 1 s GOP, 350 kbit/s) with a
burned-in camera name and clock, a moving subject crossing each camera at a known time, known
clock errors (0 s, +47 s, −312 s) and one camera whose clock is set back 15 minutes
mid-recording. It writes them into:

| Image | Scenario |
| --- | --- |
| `dahua_intact` | DHAV frames of all channels interleaved in time order |
| `dahua_overwritten` | 25% of the video region overwritten by later recording, torn frame at the boundary |
| `dahua_no_overlay` | Three overlay-less cameras, identical scene, different sensor fingerprints |
| `dahua_no_overlay_control` | As above with no fingerprint (control) |
| `hikvision_intact` | Master sector, logs, HIKBTREE + backup, data blocks with OFNI tables |
| `hikvision_formatted` | Index reset by format, video blocks untouched, format in logs |
| `hikvision_overwritten` | Formatted, then recording resumed from block 0 |

Format fidelity is checked by tools we did not write: FFmpeg's DHAV and MPEG-PS demuxers
decode every export, and an independent Hikvision parser (akira7799/hikvision-dvr-parser)
reads the Hikvision images (`tests/verify_testkit.py`). Builds are byte-reproducible.

## 2. Metrics

- **Recovery rate**: surviving ground-truth frames recovered / surviving frames.
- **False positives**: recovered units that are not a surviving ground-truth frame.
- **Temporal accuracy**: recovered frames with a time within 1 s of truth (native: recorder
  time; universal: the time the camera's own clock displayed). "Timed" counts frames given a time.
- **Camera accuracy**: correctly attributed frames among those attributed (sensor clusters are
  mapped to the majority camera, the standard clustering accuracy).

## 3. Results

| Image | Mode | Recovered | Recovery rate | False positives | Timed | Temporal accuracy (±1 s) | Camera accuracy (attributed) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dahua_intact.img | native | 5400/5400 | 100.00% | 0 | 5400 | 100.00% | 100.00% (5400) |
| dahua_no_overlay.img | native | 1800/1800 | 100.00% | 0 | 1800 | 100.00% | 100.00% (1800) |
| dahua_no_overlay_control.img | native | 1800/1800 | 100.00% | 0 | 1800 | 100.00% | 100.00% (1800) |
| dahua_overwritten.img | native | 5244/5244 | 100.00% | 0 | 5244 | 100.00% | 100.00% (5244) |
| hikvision_formatted.img | native | 5400/5400 | 100.00% | 0 | 5400 | 100.00% | 100.00% (5400) |
| hikvision_intact.img | native | 5400/5400 | 100.00% | 0 | 5400 | 100.00% | 100.00% (5400) |
| hikvision_overwritten.img | native | 5401/5401 | 100.00% | 0 | 5390 | 100.00% | 100.00% (5390) |
| dahua_intact.img | universal | 5400/5400 | 100.00% | 0 | 4540 | 99.78% | 99.81% (5400) |
| dahua_no_overlay.img | universal | 1800/1800 | 100.00% | 0 | 0 | n/a | 100.00% (1800) |
| dahua_no_overlay_control.img | universal | 1800/1800 | 100.00% | 0 | 0 | n/a | n/a (0) |
| dahua_overwritten.img | universal | 5244/5244 | 100.00% | 1 | 4884 | 99.61% | 99.64% (5244) |
| hikvision_formatted.img | universal | 5400/5400 | 100.00% | 0 | 4330 | 99.77% | 99.81% (5360) |
| hikvision_intact.img | universal | 5400/5400 | 100.00% | 0 | 4330 | 99.77% | 99.81% (5360) |
| hikvision_overwritten.img | universal | 5401/5401 | 100.00% | 0 | 4371 | 98.60% | 98.90% (5341) |

Notes: the universal false positive on `dahua_overwritten` is a torn fragment at the overwrite
boundary. Universal mode leaves 7–20% of frames untimed rather than guessing. On the control
image the tool refuses attribution (0 attributed), as intended.

## 4. Anti-forensics and clock checks

| Check | Expected | Result |
| --- | --- | --- |
| Format event in system log (Hikvision formatted, overwritten) | critical finding | detected |
| Video not in live index after format | critical finding with frame count | detected (5,400 frames formatted; 4,190 overwritten) |
| Torn frame at overwrite boundary (Dahua) | warning | detected |
| Clock drift CAM-02 / CAM-03 | +47 s / −312 s | +47 s / −312 s |
| Clock change CAM-01 at 21:16:00 | +0 → −900 s | +0 → −900 s, located between 21:15:58 and 21:16:07 |
| No clock-change false alarm on CAM-02 / CAM-03 | none | none |
| Motion triage, subject crossings | CAM-01 30–34 s, CAM-02 75–79 s, CAM-03 130–134 s | 30.0–33.75 s, 75.25–78.75 s, **missed on CAM-03** (full-frame moving texture) |

## 5. Integrity checks (automated tests)

`python -m pytest -q` runs 20 tests: exact native recovery on every image, evidence levels,
format detection, torn frames, universal attribution, clock drift and change, sensor
fingerprint with control, custody-ledger tamper detection, acquisition hashing, E01 round
trip (via the pipeline), live acquisition against a mock recorder (ONVIF WS-Security, HTTP
digest, time-filtered downloads), the UI, and OCR label consensus on real noisy readings.

## 6. Limitations and required real-world validation

- Synthetic pictures: overlay OCR, sensor fingerprints and motion triage behave differently on
  real cameras (night IR, compression, lens distortion). Expect lower accuracy.
- Real recorder firmware may differ from the modelled structures (Hikvision data blocks may
  hold raw H.264/H.265 rather than MPEG-PS; Dahua DHFS index not parsed).
- AI detectors are unvalidated; live acquisition is tested against a mock device only.

**Protocol for real drives** (per recorder model):
1. Set the recorder clock against network time; record 30 minutes on all channels with a
   phone showing network time in each camera's view; log every action with its time.
2. Image the disk (E01). Then: delete a recording, image; format, image; record 30 more
   minutes, image.
3. Run `sarvadristhi analyze` on each image; compare recovered clips and times against the action log
   and the phone clock visible in the footage.
4. Report recovery rate, temporal accuracy and false positives per model in this format, and
   compare with Rzayeva et al. (2025): 91.8% recovery, 96.7% temporal accuracy, 2.4% false
   positives on 27 drives.
