# Sarvadristhi — Execution report and limitations

As of 5 October 2026 · Windows 11 laptop (RTX 4050 6 GB) · repository `aditya36900/ProjectR`

Sarvadristhi runs natively on Windows and, in one run over three seized items, catches all 8 deliberately planted
manipulations on real CCTV footage, recovers a deleted export that is the only surviving copy of 41 s
of destroyed recordings, and produces a hashed, custody-logged report. Everything below was executed and
checked on this machine. **Nothing has yet been tested on a physical recorder disk or a real seized USB
stick**; that is the most important remaining step.

## 1. What was executed

| # | Work | Result |
|---|---|---|
| 1 | **Windows port** of the original Linux tool: no `os.pread`, no `O_BINARY`, physical drives cannot be memory-mapped, Tesseract temp files, cp1252 text encoding, GTK DLL clash with Tesseract, path separators (all marked `# Windows:`) | All original tests pass on Windows; Linux behaviour unchanged |
| 2 | **Toolchain**: Python 3.12, FFmpeg 9 (libx264), Tesseract 5.4, Pango via MSYS2, PyTorch 2.11 + CUDA 12.8, RF-DETR, YuNet/SFace, Ollama + Qwen3-VL 4B, The Sleuth Kit (pytsk3) | Installed and scripted in `scripts\setup_windows.ps1` |
| 3 | **USB / removable-media recovery** (`sarvadristhi/fsrecover.py`): lists live and deleted files; dates deletions from the NTFS change journal (`$UsnJrnl:$J`); four integrity checks per recovered file (cluster reuse, length, signature, video decode) | Deleted clip recovered intact; deletion time within 3 ms of ground truth |
| 4 | **Video-evidence workflow** (`sarvadristhi/media.py`): USB images, export folders, single videos; camera and start time from overlay OCR; disguised-file detection (header vs extension) | Disguised MP4 named `.txt` found and analysed |
| 5 | **Multi-evidence cases** (`sarvadristhi/multicase.py`): several seized items in one case on one recorder clock, unique camera IDs, merged findings, per-item custody, format coverage labels | 3-item case runs in about 10 minutes |
| 6 | **Export provenance** (`sarvadristhi/provenance.py`): every export frame matched bit-for-bit (H.264 VCL NAL SHA-1) against recorder frames | Detects copies, removed seconds and only-surviving-copy segments |
| 7 | **AI layers** (`sarvadristhi/ai/`): L1 motion · L2 RF-DETR detection + tracking + faces + colour signatures · L3 Qwen3-VL descriptions cross-checked against L2 · L4 cross-camera correlation, context rules, activity rules, narrative whose every sentence is machine-checked | Scored against MEVA human labels (section 3) |
| 8 | **Forensic workbench UI** (`sarvadristhi/ui_static/app.html`, `sarvadristhi/ui.py`): proof inspector (lineage), synchronised video wall with AI boxes, disk map + read-only hex viewer, provenance, clock chart, AI reasoning, examiner review of AI leads, custody chain; analyses run as separate processes from a queue; structured stage events | All screens exercised; no console errors |
| 9 | **Poisoned-evidence test bench** (`evidence_builder/scenario.py`): real MEVA footage written into a Hikvision-layout DVR, a Dahua-layout DVR and an NTFS USB image, with 8 planted manipulations recorded in `scenario.json` | Section 2 |
| 10 | **Reproducibility fix**: x264 with several threads plus rate control produced different bytes on every build, making the test kit non-reproducible and two tests flaky. Encoders now use one thread | Two consecutive builds byte-identical (20/20 files); full test suite passed in three consecutive runs: 19 passed, 1 skipped each (E01, needs ewf-tools) |
| 11 | **Packaging**: demo data (3.9 GB) as GitHub Release assets with a SHA-256 manifest; setup, data-build and demo scripts; A-to-Z guide | `docs/RUN_A_TO_Z.md` |

## 2. The one-run demonstration (case OP-2013, included)

**Evidence:** real footage from six surveillance cameras (MEVA dataset, Kitware/IARPA, CC-BY-4.0,
2018-03-07 16:50–16:53). The recorder disks, the USB stick and every manipulation are simulated in vendor
formats and stated as such in the UI and report.

| Item | Format identified (confidence) | Frames recovered | Coverage label |
|---|---|---|---|
| DVR-A | Hikvision (0.99) | 7,245: 2,300 index-confirmed, 4,900 carved, 45 content-only | tested on modelled images |
| DVR-B | Dahua / CP Plus (0.95) | 3,299 carved | tested on modelled images |
| USB | NTFS removable media (1.0) | 1,460 (3 videos) | external tool, widely validated |

| # | Planted manipulation | Caught | How |
|---|---|---|---|
| P1 | DVR-A formatted after the incident | Yes | Recorder log: initialise at 16:58:06 |
| P2 | Recording resumed and overwrote the earliest footage | Yes | 4,900 pre-format frames carved from unindexed blocks |
| P3 | CAM-03 clock set back 15 minutes mid-recording | Yes | Overlay-to-recorder offset steps 0 → −900 s at 16:52 |
| P4 | Camera clocks drift | Yes | +47 s (CAM-02), −312 s (CAM-04), +12 s (DVR-B CAM-02) |
| P5 | Part of the Dahua disk overwritten | Yes | Torn DHAV frame at the overwrite boundary |
| P6 | USB export of the vehicle entry deleted | Yes | Recovered intact; deleted 17:51:07.410 UTC per `$UsnJrnl`; **only surviving copy of 41 s** of CAM-1 that the DVR format destroyed; 200 frames bit-identical to DVR-A |
| P7 | USB export edited | Yes | **6.0 s removed (16:51:25–16:51:31)**, located frame by frame |
| P8 | Video disguised as `maintenance_log.txt` | Yes | MP4 header under a `.txt` name; exact copy of CAM-04 16:51:18–16:51:48 |

All 13 requirements on the case's flag board are demonstrated. Custody: chain intact; all
three evidence hashes identical before and after analysis.

## 3. AI accuracy, measured

| Measure | Value | Note |
|---|---|---|
| Activity recall against MEVA human labels | **44%** (4 of 9 scorable labels) | Hits: vehicle starting, vehicle reversing, vehicle turning, door opening. Misses: entering a vehicle, entering/exiting a facility |
| Labels falling in destroyed footage | 4 | Correctly reported as not scorable rather than as misses |
| Activities not modelled | 4 | Object transfer, purchasing, reading, texting |
| Precision (lower bound) | 4% | 187 activity events against labels that cover scripted activities only; unlabelled events are not proven false |
| VLM agreement with detector | 13 of 13 described tracks agree on class | Disagreements are flagged for review, never resolved silently |
| Narrative | 7 of 7 sentences pass the citation / time / number check | Sentences that fail are excluded from the report |

## 4. Limitations

**Evidence and formats**
- No physical recorder disk or real seized USB has been analysed. All disk images are modelled: real video in vendor-format containers built by the evidence_builder.
- Native parsing covers 3 OEMs (Hikvision; Dahua and CP Plus, which shares Dahua's format). Honeywell, TP-Link, Uniview, Matrix and Godrej are handled only by generic carving and overlay OCR; none has been tested on a real unit.
- The Hikvision structures follow published research and an MIT parser derived from one real DVR (firmware HIK.2011.03.08). Newer firmware may differ.
- Dahua's DHFS index is not parsed, so Dahua footage is never "index-confirmed".
- H.265 is supported (section 4b) but validated on evidence_builder footage only; universal-mode attribution on H.265 is weaker (74–90%).
- Honeywell support follows one published study of one NVR model; the Video Channel List is not parsed.
- The recorder model and firmware are not identified; only the format family is.
- E01 images can be read but not written on Windows (ewf-tools are Linux-only).
- On Windows a physical drive must be imaged first (Administrator terminal), then analysed. Imaging a real drive on Windows has not been tested.
- The NTFS deletion time requires the change journal; Windows does not always enable it on USB sticks. FAT32 and exFAT give only a bound ("deleted after last modification").

**AI**
- Activity recall is 44% on 9 labels: a small sample. Entering a vehicle was missed in the clip where it happened.
- Parked cars fragment into many tracks (143 "moving objects" on one road camera), and door entries are confused with occlusions, which keeps precision low.
- Cross-camera links are hypotheses for an examiner, not identifications; link accuracy has not been measured.
- Faces are detected and embedded, but there is no face search across a case.
- Qwen3-VL 4B can mislabel vehicle types; the L3 cross-check flags this.
- AI output is excluded from the s.63(4) data sheet; only leads an examiner accepts become findings.

**Engineering**
- No unit tests yet for the new modules (`fsrecover`, `provenance`, `multicase`, `ai`, job queue); they are exercised by the demo runs only.
- The scenario data in the release was built before the encoder fix: it is internally consistent and verified by manifest, but a rebuild produces new (reproducible) bytes. NTFS images are never byte-reproducible (filesystem timestamps).
- Architecture, SOP, user manual and validation documents predate this work; the final project report is not written.
- The workbench is local only (127.0.0.1) with no user accounts. Chrome does not load video in background tabs.

## 4b. Update: workbench, H.265, Honeywell, replay (branch `feature/light-workbench`)

Executed on a second Windows 11 laptop (AMD Radeon 740M, no CUDA). Test suite: 34 tests
(33 passed, 1 skipped: E01 needs Linux ewf-tools).

| Work | Result |
|---|---|
| **H.265 end to end** (`formats/codec.py`): codec decided per stream from parameter sets; HEVC access-unit carving; Hikvision PS keyframes, DHAV, export (`-f hevc`), OCR decoding and provenance hashing made codec-aware; evidence_builder renders x265 (`python -m evidence_builder.h265`) | Native Hikvision (intact, formatted) and Dahua (intact, overwritten) H.265: 100% recovered, 0 false positives, 100% time and camera, index levels correct. Universal on H.265: 100% recovered, 0 false positives, 74–90% time/camera (OCR on low-bitrate x265 test footage) |
| **Honeywell native plugin** (`formats/honeywell.py`, `plugins/honeywell.py`) from Yoon & Hwang, DFRWS USA 2026, section 5.4.6, read in full | Known-answer check: the paper's timestamp 0x000644865C1BCEE6 decodes to its stated 2025-11-26 21:48:41.896. Testkit Honeywell layout (`python -m evidence_builder.honeywell`): 100% recovered, 0 false positives, 100% time, 99% camera (overlay OCR); torn records at an overwrite boundary reported. H.265 variant: 100% / 0 / 100%, camera 76% |
| **Replay capsule** (`capsule.py`, `ed25519.py`, `sarvadristhi capsule`, `sarvadristhi replay`) | OP-2013: 14/14 clips (recorder streams and USB files) rebuilt from 11,214 byte ranges; build 3.5 s, replay 1.2 s; a flipped byte fails exactly one clip; an edited capsule fails its signature. Ed25519 passes RFC 8032 vectors 1 and 2 |
| **Format hypotheses** with brand-string conflicts (`workbench.py`, `evidence_builder/labels.py`) | A Hikvision disk carrying a "CP PLUS" string is still identified as Hikvision; the label is shown as a conflict |
| **Evidence reconciliation** per camera: index, frames on disk, log, overlay clock, exports | OP-2013: the format event surfaces as "discrepant" on all four DVR-A cameras; the 6.0 s edit and the only-surviving-copy export appear in the export column |
| **Ask the case**: answers built only from stored detections, activities and findings, with citations; identity and unmatched questions refused | Deterministic; no generated text |
| **Export integrity without a recorder copy** (`integrity.py`): parameter-set change, timestamp gap, GOP cadence | Flags a constructed splice at the exact frame and a 1 s cut; the scenario's edited export is structurally clean (a keyframe-aligned stream-copy cut), which is why provenance against the recorder is still needed |
| **Light workbench UI** rewritten (`ui_static/app.html`) with capability matrix, live ingest, timeline robustness, replay panel, proof drawer | All screens exercised on OP-2013 and three new demo cases |
| **Setup fix**: MSYS2 `ucrt64\bin` placed ahead of Tesseract on the user PATH | Without it, WeasyPrint loads Tesseract's older Pango and PDF reports fail (error 0x7f) |

## 4c. Update: Sarvadristhi rename, examiner tools, live acquisition, validation kit

The product is now **Sarvadristhi** (Python package `sarvadristhi`; test-evidence builder `evidence_builder`);
"dvrf" collided with an existing GitHub project. Feature modules use plain technical English names.
Test suite: 44 tests, 43 passed, 1 skipped (E01).

| Work | Result |
|---|---|
| **Frame X-ray** (`frame_xray.py`) | Per-frame offsets for every recorder clip from frames.csv; cluster runs for files on removable media; header decode for DHAV, Honeywell record, MPEG-PS/PSM/PES/PTS and H.264/H.265 NAL, checked on OP-2013 and a Honeywell case |
| **Examiner findings, Clock correction, Segment export** (`examiner.py`) | Examiner findings with captured frames, clock corrections with basis, stream-copy segments with hashes; each a custody entry; chain verifies afterwards |
| **Report** | Key frames per camera, examiner findings with frames, corrections, segments and the capsule seal; report hashes returned and logged. Fixed a template crash when a camera's drift is unknown |
| **Live acquisition** live acquisition as a job | Against the simulated recorder: device identified, clock recorded, two channels downloaded and hashed, analysed, cameras labelled by OCR; the password appears in no file or ledger entry (tested) |
| **Recorder validation** (`recorder_validation.py`, `sarvadristhi validate-recorder`) | Rehearsal on the H.265 formatted disk: 119/119 logged seconds on each of three cameras, format event detected; capability cells become "real disk" only from a physical run (tested) |
| **Standards** standards map | ISO/IEC 27037, SWGDE 17-V-002 v1.3, NIST CFTT, NIST AI RMF, BSA s.63, DPDP Act; statuses stated without claiming certification |
| **Presentation mode** | Ten steps from seizure to verdict with live case facts; keyboard and auto-advance |
| **Real-footage demo disks** (`evidence_builder.realdemos`) | MEVA footage in H.265 Hikvision (formatted), H.265 Dahua (overwritten) and Honeywell (overwritten) layouts: 100% recovered, 0 false positives, 100% time; camera 100% / 100% / 99.6% |

## 5. Next steps, in order
1. Test on a real recorder (Hikvision or CP Plus): record with a handwritten event log, delete, format, overwrite, image the disk.
2. ~~Add H.265 support~~ done (section 4b); record the real-disk test in both codecs if the DVR allows it.
3. Reduce AI noise (static-vehicle handling, occlusion-aware track ends) and measure link accuracy on annotated footage.
4. Unit tests for the new modules.
5. Update architecture, SOP, user manual and validation report; write the final project report.
