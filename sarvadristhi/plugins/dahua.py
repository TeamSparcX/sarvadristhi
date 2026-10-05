"""Dahua family (Dahua, CP Plus, Lorex, Amcrest and other OEMs): DHAV frames.

DHFS index structures are not parsed yet (layout unpublished); every frame is carved with
header+footer validation, and its recorder timestamp comes from the DHAV header."""
from ..formats import dhav
from ..models import EvidenceLevel, Finding, Frame, Recovery
from .base import Plugin

SAMPLE_WINDOWS, WINDOW = 64, 1024 * 1024


class DahuaPlugin(Plugin):
    name = "dahua"
    family = "dahua-dhav"

    def detect(self, src):
        head = src.read(0, 5)
        if head == b"DAHUA" or (head[:4] == b"DHAV" and dhav.parse_at(src.buf, 0, src.size)[0]):
            return 0.99, {"export_header": head.decode("latin-1")}
        valid, windows = 0, 0
        step = max(WINDOW, src.size // SAMPLE_WINDOWS)
        for a in range(0, src.size, step):
            n = sum(1 for _ in dhav.scan(src.buf, a, min(a + WINDOW, src.size)))
            valid += n
            windows += n > 0
        if not valid:
            return 0.0, {}
        return min(0.95, 0.5 + 0.05 * windows), {"valid_frames_sampled": valid, "windows_with_frames": windows}

    def recover(self, src) -> Recovery:
        rec = Recovery(self.family, self.name)
        start = 0x400 if src.read(0, 5) == b"DAHUA" else 0
        rejects = []
        for f in dhav.scan(src.buf, start, src.size, rejects):
            if f.type not in dhav.VIDEO_TYPES:
                continue
            rec.frames.append(Frame(f.offset, f.length, (f.payload,), f.keyframe, self.family,
                                    EvidenceLevel.CARVED_TIMESTAMPED, f.channel0 + 1, f.time_ms,
                                    "container"))
        torn = [(o, w) for o, w in rejects if w in ("footer-mismatch", "truncated")]
        rec.fragments = [(o, 0) for o, _ in torn]
        if torn:
            rec.findings.append(Finding("torn-frames", "warning",
                f"{len(torn)} DHAV frame header(s) without a matching footer: consistent with "
                f"partial overwrite or truncation", {"offsets": [o for o, _ in torn[:50]]}))
        from ..formats.codec import sniff
        first = rec.frames[:30]
        rec.metadata = {"dhfs_index": "not parsed (layout unpublished); frames carved",
                        "codec": sniff(b"".join(bytes(src.buf[o:o + n]) for f in first for o, n in f.es)) if first else None}
        return rec
