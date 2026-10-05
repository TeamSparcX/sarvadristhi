"""Honeywell NVR family: 20-byte Custom Header records (Yoon & Hwang, DFRWS USA 2026).

Time comes from each record's own microsecond timestamp (recorder-written). The channel list is
not parsed, so the camera comes from the burned-in label (overlay OCR), as in the CARVE method
from the same group (Giri, Yoon & Hwang, DFRWS APAC 2026). Frames whose label cannot be read keep
their recorder time but no camera."""
from ..formats import honeywell
from ..models import EvidenceLevel, Finding, Frame, Recovery
from .base import Plugin

SAMPLE_WINDOWS, WINDOW = 64, 1024 * 1024


class HoneywellPlugin(Plugin):
    name = "honeywell"
    family = "honeywell"

    def detect(self, src):
        valid, windows = 0, 0
        step = max(WINDOW, src.size // SAMPLE_WINDOWS)
        for a in range(0, src.size, step):
            n = 0
            for _ in honeywell.scan(src.buf, a, min(a + WINDOW, src.size)):
                n += 1
                if n >= 64:
                    break
            valid += n
            windows += n >= 3                     # a chain of records, not a chance byte match
        if not windows:
            return 0.0, {}
        return min(0.95, 0.5 + 0.05 * windows), {"valid_records_sampled": valid, "windows_with_records": windows}

    def recover(self, src, attribute: bool = True) -> Recovery:
        from ..formats.codec import sniff
        rec = Recovery(self.family, self.name)
        torn = []
        records = list(honeywell.scan(src.buf, 0, src.size, torn))
        if torn:
            rec.fragments = [(o, 0) for o in torn]
            rec.findings.append(Finding("torn-frames", "warning",
                f"{len(torn)} Honeywell record header(s) whose length does not end on a record boundary: "
                f"consistent with partial overwrite or truncation", {"offsets": torn[:50]}))
        frames = [Frame(r.offset, r.total, ((r.offset + honeywell.HDR, r.length),), r.keyframe, self.family,
                        EvidenceLevel.CARVED_TIMESTAMPED, None, r.time_ms, "container") for r in records]
        attribution = "skipped"
        if attribute and frames:
            from .. import ocr
            from .generic import GenericPlugin
            if ocr.available():
                saved = [(f.time_ms, f.time_source) for f in frames]
                attribution = GenericPlugin().attribute(src, frames)
                for f, (t, s) in zip(frames, saved):      # time stays the recorder's own; OCR gives the camera
                    f.time_ms, f.time_source = t, s
        rec.frames = frames
        first = frames[:30]
        rec.metadata = {"records": len(records), "resolutions": sorted({f"{r.width}x{r.height}" for r in records}),
                        "codec": sniff(b"".join(bytes(src.buf[o:o + n]) for f in first for o, n in f.es)) if first else None,
                        "channel_list": "not parsed; camera from overlay OCR", "attribution": attribution}
        return rec
