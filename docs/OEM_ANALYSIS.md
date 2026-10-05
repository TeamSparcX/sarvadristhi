# Comparative analysis of DVR/NVR OEMs

As of October 2026. "Verified" means checked by this project against independent tools or
published research; "unverified" means no sample has been examined yet.

## Summary

| OEM | On-disk family | Video container | Recorder timestamps | Index | Sarvadristhi path today | Status |
| --- | --- | --- | --- | --- | --- | --- |
| Hikvision | Hikvision FS: master sector `HIKVISION@HANGZHOU` at 0x200 | H.264/H.265 in data blocks (evidence_builder models MPEG-PS) | HIKBTREE entries; per-block OFNI IDR tables | HIKBTREE + backup copy | Native | Structures from published research and an MIT parser derived from a real DVR; validated on evidence_builder |
| Dahua | DHFS 4.x | DHAV frames (header + footer) | In every DHAV header (second + ms counter) | DHFS (unpublished) | Native (frames); index not parsed | DHAV layout per FFmpeg `dhav.c`; validated on evidence_builder |
| CP Plus | Dahua family (listed as a Dahua OEM; Aditya Infotech is Dahua's exclusive India distributor) | DHAV | DHAV | DHFS | Native via Dahua | Verify per model: Aditya Infotech also manufactures at Kadapa |
| Honeywell | Honeywell NVR FS (undocumented by vendor) | H.264/H.265 | Microsecond time in every 20-byte record header | Block/Channel lists (DFRWS USA 2026) | Native (records); camera from overlay OCR | Record layout from the paper, validated on evidence_builder; channel list not parsed; needs a real unit |
| Uniview | Proprietary FS | Exports as .mp4/.ts | Unverified | Unverified | Universal | Needs one unit |
| TP-Link (VIGI) | Unverified | Unverified | Unverified | Unverified | Universal | Needs one unit |
| Matrix (India) | Unverified | Unverified | Unverified | Unverified | Universal | Needs one unit; request from Matrix (Vadodara) |
| Godrej | Unverified (OEM source unknown) | Unverified | Unverified | Unverified | Universal; family detection decides | Needs one unit |

Every OEM runs end to end. What differs is metadata depth: the universal path recovers
H.264/H.265 from any recorder and attributes it from the burned-in overlay (OCR) or, without
an overlay, by sensor-noise fingerprint.

## Hikvision

- Master sector (256 bytes) at offset 0x200 with the signature `HIKVISION@HANGZHOU`, giving
  disk capacity, system-log area, video-data offset, data-block size, block count and the
  offsets of the primary and backup HIKBTREE (Han, Jeong & Lee, ICDF2C 2015; field offsets
  as refined on firmware `HIK.2011.03.08` by akira7799/hikvision-dvr-parser, MIT).
- HIKBTREE pages hold 48-byte entries per data block: status, channel, start/end time,
  block offset. A format resets these entries; video blocks remain.
- System logs (`RATS` records) include power, recording, motion and disk events; a format
  leaves an initialise entry, which Sarvadristhi reports as a critical finding.
- Each data block ends with an `OFNI` IDR table (channel, time, offset of every keyframe).
  These tables survive an index reset, so Sarvadristhi recovers footage from a formatted disk *with*
  recorder timestamps.
- Live: ISAPI `ContentMgmt/search` and `ContentMgmt/download`; ONVIF Profile G.

## Dahua and the Dahua OEM family (CP Plus, Lorex, Amcrest and others)

- Video is a sequence of DHAV frames: `DHAV` + type + channel + frame number + total length +
  packed date + millisecond counter + extensions, payload, and a `dhav` + length footer.
  Requiring header and footer to agree rejects torn frames at overwrite boundaries.
- DHFS 4.x index structures are unpublished; Sarvadristhi carves frames and keeps their recorder
  timestamps (evidence level: carved + timestamp). Parsing DHFS is the main remaining Dahua task.
- Exports: `.dav` (DHAV, sometimes behind a 0x400-byte `DAHUA` header).
- Live: `cgi-bin/loadfile.cgi` (HTTP digest) returns DHAV for a channel and time window.
- Commercial tools report weak support for DHFS; independent recovery work (Rzayeva et al.,
  MDPI Information 2025) validated header-footer validation on 27 drives.

## Honeywell

Two 2026 papers from Sungkyunkwan University document Honeywell's filesystem and deletion
behaviour (DFRWS USA 2026) and a recovery method, CARVE, that reconstructs deleted
H.264/H.265 and attributes fragments using overlay OCR (DFRWS APAC 2026).

Sarvadristhi now has a native Honeywell plugin built from section 5.4.6 of the USA 2026 paper (read in
full): each frame's data is preceded by a 20-byte little-endian Custom Header (frame type 0x82
IDR / 0x02 non-IDR, fixed `80 01 00`, width, height, data length, Unix time in microseconds),
and each channel's data ends with 20 zero bytes. Video sits from offset 0x80000000 of
Partition 1. Records are found by structure, so formatting (which erases the Header and the
block lists) does not prevent recovery, as the paper shows. The Video Block List, Video Channel
List and Record State areas are not parsed, so the camera comes from overlay OCR (the CARVE
approach). The paper studied one model (HN35080200 NVR); other models must be checked.

## Uniview, TP-Link, Matrix, Godrej

No samples have been examined. Sarvadristhi handles them through format-family detection (a rebadged
unit that writes Hikvision or Dahua structures is identified as such) and the universal path.
Each needs one real unit to (1) image, (2) record a controlled event log, and (3) write and
validate a native plugin through the documented plugin contract.

## Cross-OEM observations

- Rebranding is common, so brand is weak evidence of format.
- All examined families store H.264/H.265 elementary video, so vendor-agnostic carving always
  applies; vendors differ in container framing, index structures and where time is stored.
- Burned-in overlays (camera name, clock) are near-universal on DVRs and give an independent
  time and camera source, which is also how clock tampering is detected.
- Recorder clocks are frequently wrong; the offset must be recorded at seizure.

## Sources

- Han, Jeong & Lee, *Analysis of the HIKVISION DVR File System*, ICDF2C 2015.
- akira7799/hikvision-dvr-parser (GitHub, MIT).
- FFmpeg `libavformat/dhav.c`.
- Rzayeva et al., *Automated Forensic Recovery Methodology for Video Evidence from Hikvision and
  Dahua DVR/NVR Systems*, MDPI Information, 2025.
- Yoon & Hwang, *Forensic analysis of video data deletion and recovery in Honeywell surveillance
  file system*, DFRWS USA 2026.
- Giri, Yoon & Hwang, *CARVE: Recovering and Reconstructing Deleted H.264/H.265 Video from
  Honeywell Surveillance Systems*, DFRWS APAC 2026.
- SecurityCamCenter, *Dahua OEM List (2026)*; Aditya Infotech IPO notes (SBI Securities, ICICI Direct).
