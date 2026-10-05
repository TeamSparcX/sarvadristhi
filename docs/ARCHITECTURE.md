# Sarvadristhi architecture

## Design rules

1. **Identify the format, never trust the brand label.** Recorders are widely rebranded
   (CP Plus ships Dahua-family firmware; many brands OEM Hikvision or Dahua). Detection
   reads signatures and frame structure from the disk.
2. **Never trust the index alone.** Native parsers read the recorder's index, but every
   data area is also walked and carved, and a vendor-agnostic carver sweeps every byte the
   native parser did not claim. Formatted and overwritten disks are the normal case in
   tampering investigations.
3. **Say how strong each fact is.** Every frame carries an evidence level and a time source.
   Machine triage is kept separate from findings.
4. **Import, don't rebuild.** FFmpeg decodes, Tesseract reads text, OpenCV analyses images,
   libewf handles E01, WeasyPrint renders PDF. Sarvadristhi's own code is the forensic glue.
5. **Everything is reproducible.** Inputs are hashed before and after, every step goes to a
   hash-chained ledger, and the test evidence builds byte-identically.

## Pipeline

```mermaid
flowchart TD
    A[Physical disk via write-blocker] --> ACQ[acquire: raw or E01, hashes, ledger]
    L[Running NVR] --> LIVE[live: ONVIF identity + clock, vendor download]
    X[Export file .dav / .ps] --> REG
    ACQ --> REG[register: hash before]
    LIVE --> REG
    REG --> DET[detect: format family + confidence]
    DET --> NAT[native plugin: index + structured walk]
    DET -->|unknown family| UNI[universal: carve H.264 / MPEG-PS]
    NAT --> SWEEP[universal sweep of unclaimed bytes]
    UNI --> ATT[attribution: overlay OCR, label clustering, template match, temporal sequencing, sensor fingerprint]
    NAT --> DRIFT[clock drift + clock change]
    SWEEP --> CLIPS
    ATT --> CLIPS[clips: group, export MP4 + raw, hash]
    DRIFT --> CLIPS
    CLIPS --> TRI[triage: motion, optional AI]
    CLIPS --> TL[timeline + anti-forensic findings]
    TRI --> REP
    TL --> REP[report: PDF/HTML, s.63(4) data sheet, custody annex; hash after]
```

## Modules

| Module | Responsibility |
| --- | --- |
| `source` | Read-only, memory-mapped access to an image or block device |
| `acquire`, `ewf` | Raw imaging with sector-level retry; E01 via ewfacquire/ewfverify; E01 expansion verified against stored hashes |
| `live` | ONVIF (WS-Security digest): device info, system time, recording search, replay URI; Dahua `loadfile.cgi` and Hikvision ISAPI downloads |
| `custody` | Append-only JSON-lines ledger; each entry carries the SHA-256 of the previous one |
| `detect` | Runs every plugin's `detect`, ranks families |
| `formats/` | DHAV (header + footer validation), MPEG-PS walking, raw H.264 access-unit carving |
| `plugins/hikvision` | Master sector, system logs, HIKBTREE (primary and backup), per-block OFNI IDR tables |
| `plugins/dahua` | DHAV frames with recorder timestamps; torn-frame detection |
| `plugins/generic` | Universal carving and attribution for any recorder |
| `ocr` | Locate overlay regions once, then batch-OCR small crops (Tesseract) |
| `prnu` | Sensor-noise fingerprint clustering for overlay-less footage, with a bimodality test |
| `clockdrift` | Overlay clock vs recorder time per camera; sustained-step clock-change detection |
| `clips`, `timeline` | Grouping, export, recording gaps, combined timeline |
| `analytics/` | Motion triage (overlay masked), AI triage with local models and OpenCV fallbacks |
| `report/` | Jinja2 template, SVG timeline, WeasyPrint PDF |
| `pipeline` | One case run end to end |
| `ui` | Local browser UI (127.0.0.1 only) |
| `validate` | Scores recovery against pramanshala ground truth |

## Common data model

```text
Frame    offset, length, es[(offset,length)...], keyframe, family,
         level (index-confirmed | carved + timestamp | content-only),
         channel, time_ms, time_source (index | container | ofni | overlay | none), label
Finding  kind, severity (critical | warning | info), summary, detail
Recovery family, plugin, frames, findings, metadata, fragments
Clip     id, channel, label, frames -> start/end, level, fps, exported files + SHA-256
```

## Adding a recorder family

Implement `sarvadristhi.plugins.base.Plugin`:

```python
class NewPlugin(Plugin):
    name, family = "newvendor", "newvendor-fs"
    def detect(self, src):   # -> (confidence 0..1, evidence dict); read signatures, never labels
    def recover(self, src):  # -> Recovery with Frames mapped to the common model
```

Register it in `sarvadristhi/plugins/__init__.py` (`NATIVE`). Add a pramanshala generator for the
format (or, better, a real image with a documented event log) and a validation test.
Everything downstream (sweep, attribution, clips, timeline, report) works unchanged.

## Security notes

- Evidence is opened `O_RDONLY` and memory-mapped read-only; hashes are compared before and
  after analysis. A hardware write-blocker is still required for original media.
- The UI binds to 127.0.0.1 and rejects paths outside the case folder.
- Live acquisition performs only read operations (identity, time, search, download).
- No network access is needed for analysis; optional AI models run locally.
