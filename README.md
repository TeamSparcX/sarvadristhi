# Sarvadristhi — multi-vendor DVR/NVR forensic analysis (SIH26150)

> **Start here (Windows):** [docs/RUN_A_TO_Z.md](docs/RUN_A_TO_Z.md) — setup, demo data, running the
> Forensic Workbench and the one-run "Operation demo".
> `scripts\setup_windows.ps1` once, then `scripts\run_demo.ps1`.
> **New contributor?** [docs/CONTRIBUTOR_GUIDE.md](docs/CONTRIBUTOR_GUIDE.md) — accept the invite, download, run, contribute.
> **What was done and its limits:** [docs/EXECUTION_REPORT.md](docs/EXECUTION_REPORT.md).
> Demo data (3.9 GB: real CCTV footage, poisoned DVR/USB images, models, a finished case) is attached to
> the GitHub Release `v1.0-data`; `scripts/DATA_MANIFEST.json` holds the SHA-256 of every file.

`sarvadristhi` takes a recorder hard disk (or its image, or an export file), identifies the
on-disk format from its bytes, recovers every frame of video it can — including footage
from a formatted or partly overwritten disk — attributes each frame to a camera and a
time, and produces a hashed, custody-logged court report with a data sheet for the
BSA s.63(4) certificate.

`pramanshala/` builds vendor-exact test evidence with frame-level ground truth, so every
claim below is measured, not asserted.

## Measured results (dvr-pramanshala 0.4.0)

Native mode uses the vendor parser; universal mode ignores it and works from raw video plus
the burned-in overlay or, without an overlay, the sensor-noise fingerprint.

| Scenario | Native: recovered / time / camera | Universal: recovered / time / camera |
| --- | --- | --- |
| Dahua intact | 100% / 100% / 100% | 100% / 99.8% / 99.8% |
| Dahua overwritten | 100% / 100% / 100% | 100% / 99.6% / 99.6% |
| Hikvision intact | 100% / 100% / 100% | 100% / 99.8% / 99.8% |
| Hikvision formatted | 100% / 100% / 100% | 100% / 99.8% / 99.8% |
| Hikvision formatted + overwritten | 100% / 100% / 100% | 100% / 98.6% / 98.9% |
| No overlay, identical scene, sensor fingerprints | 100% / 100% / 100% | 100% / untimed / 100% |
| Same without fingerprints (control) | 100% / 100% / 100% | 100% / untimed / refuses to attribute |

Native mode has zero false positives; universal mode has one (a torn fragment). Universal
mode leaves 7–20% of frames untimed rather than guessing. Full results, metrics and the
anti-forensic checks (format, index reset, torn frames, clock drift, clock change) are in
`docs/VALIDATION_REPORT.md`.

**These are synthetic pictures in real container formats. Repeat every number on real
drives before presenting it as a real-world result.**

## Quick start

Requires Python 3.10+, FFmpeg (libx264), Tesseract; WeasyPrint for PDF.

```bash
pip install -e ".[pdf,e01,test]"                # plus ffmpeg, tesseract, ewf-tools on the system
python -m pramanshala kit/                          # build test evidence (~1 min)
python -m sarvadristhi ui --cases cases/                # browser UI on 127.0.0.1
python -m sarvadristhi analyze kit/images/hikvision_overwritten.img --case case1 --case-id C-1 --examiner "Name"
python -m sarvadristhi verify-custody case1
python -m sarvadristhi validate kit/ --universal --out results/
python -m pramanshala.h265 kit/h265                 # H.265 Dahua + Hikvision images with ground truth
python -m pramanshala.honeywell kit/honeywell        # Honeywell-layout images (record format per DFRWS USA 2026)
python -m pramanshala.labels kit/images/hikvision_intact.img kit/images/cpplus_label_on_hikvision.img
python -m sarvadristhi capsule case1                     # seal a replay capsule (byte ranges of every clip, Ed25519)
python -m sarvadristhi replay case1/capsule              # rebuild every clip from the image and compare hashes
python -m pramanshala.realdemos kit/meva kit/scenario kit/realdemo   # the same disks from real MEVA footage
python -m sarvadristhi kasauti DISK.img --events log.csv --out run1 --physical --make Hikvision --model ...
python -m sarvadristhi kasauti-template          # event-log template for a real-recorder session
python -m pytest -q                             # 38 tests, ~6 min (Windows: add --basetemp C:\Work\pytest-tmp)
```

Other commands: `detect`, `acquire` (raw or `--format e01`), `live` (pull from a running
recorder), `analyze --ai`, `analyze --mode universal`. See `docs/USER_MANUAL.md`.
A case folder holds `report.pdf`, `report.html`, `case.json`, `frames.csv`, `clips/` (MP4 +
raw H.264, each hashed) and `custody.jsonl`.

## How it works

1. **Acquire or register** the source read-only; hash it (MD5 + SHA-256) and log it.
2. **Identify** the format family from signatures and frame structure (never the brand label).
3. **Recover**: the family's native parser runs first; a vendor-agnostic H.264 / H.265 carver
   then sweeps every byte the parser did not claim. The codec of each stream is decided once,
   from its parameter sets.
4. **Attribute**: camera and time come from the container where the recorder wrote them;
   otherwise from OCR of the burned-in label and clock, with label clustering, template
   matching and temporal sequencing to correct OCR slips.
5. **Check clocks**: overlay vs recorder time per camera; sustained steps flag a clock change.
6. **Export** clips as MP4 and raw H.264/H.265 with SHA-256; **triage** motion (overlay masked).
7. **Report** and re-hash the source to prove it was not changed.
8. **Seal and replay**: a replay capsule lists the exact byte ranges behind every clip, signed
   with Ed25519; anyone with the image rebuilds each clip and checks its hash without Sarvadristhi's parsers.

## Workbench (`python -m sarvadristhi ui`)

A light, offline browser workbench on 127.0.0.1.

| Module | What it does |
|---|---|
| **Case overview** | Case overview: a still from the key camera, the verdict panel, evidence cards, camera rail, findings, PS flags |
| **Frame X-ray** | Frame-to-byte X-ray: while a clip plays, a disk ribbon and a hex panel follow the frame on screen, showing where its bytes are on the seized image and decoding the recorder's own header (DHAV, Honeywell record, MPEG-PS/PES, NAL) |
| **Presentation** | Presentation mode: ten guided steps from seizure to verdict, with live case facts (`#/present/<case>`) |
| **Format hypotheses** | Format hypotheses: every parser's interpretation of each disk with the checks it passes and fails; brand strings shown as weak context and as conflicts |
| **Reconciliation** | Reconciliation: recorder index, frames on disk, recorder log, burned-in clock and exports, camera by camera |
| **Clock analysis** | Clocks: measured drift and clock changes, and how far a clock must be wrong to reverse the order of events |
| **Clock correction** | Examiner clock correction (recorder to real-world time) with its basis; recorder times are never rewritten |
| **Ask the case** | Ask the case: answers only from stored detections, activities and findings, with citations; identity and ungrounded questions refused |
| **Examiner findings** | Findings the examiner writes at the playhead, with the frame captured, kept apart from AI output |
| **Segment export** | Segment export without re-encoding, hashed, as a labelled derivative |
| **Custody chain** | The hash-chained custody ledger, re-verified on open |
| **Replay capsule** | Replay capsule: byte ranges behind every clip, Ed25519-signed; rebuild and compare, or flip a byte to watch replay catch it |
| **Live acquisition** | Live acquisition from a running recorder (ONVIF, Dahua and Hikvision download APIs), password never written to disk; a simulated recorder for demos |
| **Recorder validation** | Real-recorder validation: score an image against a handwritten event log; a passing physical run promotes the format to "real disk" |
| **Capability matrix** | Capability matrix per OEM and function, with sources and trust levels |
| **Standards** | How each step answers ISO/IEC 27037, SWGDE DVR acquisition guidance, NIST CFTT, NIST AI RMF, BSA s.63 and the DPDP Act |
| **Live ingest** | Live ingest view while an analysis runs |
| **Pramanshala** | The test-evidence builder (`python -m pramanshala ...`) |

Regenerated reports now include key frames per camera, Examiner findings findings with their frames, Clock correction
corrections, Segment export segments and the Replay capsule seal, and the report's own hashes are logged to custody.

Every frame carries an evidence level: **index-confirmed** (in the recorder's live index),
**carved + timestamp** (recovered, with a recorder-written timestamp), or **content-only**
(camera/time from the picture, or unknown).

## OEM coverage

| OEM | Path today | What is needed for more |
| --- | --- | --- |
| Hikvision | Native: master sector, logs, HIKBTREE, OFNI IDR tables; H.264 and H.265 | Real drives across firmware versions |
| Dahua | Native: DHAV frames with recorder timestamps; H.264 and H.265 | DHFS index parsing (needs real drives) |
| CP Plus | Native via Dahua family (listed as a Dahua OEM) | Verify model by model |
| Honeywell | Native: 20-byte record headers with microsecond time (Yoon & Hwang, DFRWS USA 2026); camera from overlay OCR | Channel list parsing; a real unit |
| Uniview, TP-Link, Matrix, Godrej | Universal: H.264/H.265 carve + overlay OCR | One real unit each to write a native plugin |

H.265, measured on the pramanshala (40–120 s per camera, 3 cameras): native Hikvision, Dahua and Honeywell
recover 100% of surviving frames with 0 false positives and 100% correct recorder times. Universal mode
recovers 100% with 0 false positives, but attributes 74–90% of frames to time and camera (H.264: 99.8%),
because overlay OCR reads fewer labels and clocks on low-bitrate H.265 test footage.

## Status

| Component | State |
| --- | --- |
| Detection, Hikvision + Dahua plugins, universal carver, orchestrator | Built, validated on pramanshala |
| Overlay OCR, label clustering, template match, temporal sequencing | Built, validated on pramanshala |
| Sensor-fingerprint (PRNU-style) attribution with control | Built, validated on pramanshala |
| Clock drift and clock-change detection | Built, validated (true positive, no false alarms) |
| Raw and E01 acquisition, E01 input, custody ledger | Built, tested |
| Live acquisition: ONVIF + Dahua/Hikvision download APIs | Built, tested against a mock recorder only |
| Clip export, report + s.63(4) data sheet, local UI | Built, tested |
| Motion triage | Built; misses objects over full-frame moving texture |
| AI triage (RF-DETR, YuNet, Qwen3-VL; OpenCV fallbacks) | Built; runs end to end; **model accuracy unvalidated** |
| H.265 (native and universal paths, export, provenance, OCR) | Built, validated on pramanshala |
| Honeywell native plugin (record headers, torn-record detection) | Built from the paper, validated on pramanshala and the paper's sample timestamp |
| Replay capsule with Ed25519 seal (RFC 8032 vectors pass) | Built, tested (rebuild, tamper, edited capsule) |
| Export integrity without a recorder copy (parameter-set change, timestamp gap, GOP cadence) | Built, tested on constructed splices and cuts |
| Native plugins for Uniview, TP-Link, Matrix, Godrej | Not possible without samples (universal path covers them) |
| Validation on real drives and real NVRs | **Not done: highest priority** |

## Documents (PS deliverables)

| Deliverable | File |
| --- | --- |
| Comparative analysis of OEMs | `docs/OEM_ANALYSIS.md` |
| System architecture | `docs/ARCHITECTURE.md` |
| Standard operating procedure | `docs/SOP.md` |
| User manual | `docs/USER_MANUAL.md` |
| Validation report | `docs/VALIDATION_REPORT.md` |
| Forensic images | built reproducibly by `python -m pramanshala` (real-drive images: pending) |
| Functional prototype | this repository; sample report in `sample_output/` |

## Layout

```
sarvadristhi/            formats/ (DHAV, MPEG-PS, H.264, codec/H.265, honeywell)  plugins/ (hikvision, dahua, honeywell, universal)
                 detect, recover, ocr, prnu, clockdrift, clips, timeline, analytics/,
                 report/, acquire, ewf, live, custody, pipeline, validate, ui,
                 capsule + ed25519 (replay), workbench (hypotheses, reconciliation, ask), integrity
docs/            OEM analysis, architecture, SOP, user manual, validation report
pramanshala/         reproducible test evidence with ground truth (H.264, h265, honeywell, labels)
tests/           test_dvrf.py + test_workbench.py (pytest), verify_testkit.py (independent-tool checks), mock_nvr.py
sample_output/   validation table and a sample case report
```

## Credits and licences

Format knowledge: FFmpeg `dhav.c` (LGPL); Han, Jeong & Lee, ICDF2C 2015;
akira7799/hikvision-dvr-parser (MIT, structure reference and independent verifier);
CARVE (Giri, Yoon & Hwang, DFRWS APAC 2026) for the overlay-attribution approach;
Yoon & Hwang, DFRWS USA 2026 (arXiv 2605.07430) for the Honeywell record header; RFC 8032 for Ed25519.
Runtime: NumPy, OpenCV, Jinja2, WeasyPrint, FFmpeg, Tesseract, libewf. Optional: RF-DETR,
YuNet, Qwen3-VL via Ollama. No AGPL components.
Choose and add a licence for this repository before publishing it.
