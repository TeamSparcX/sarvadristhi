"""MPEG-2 Program Stream walking: frames = pack + PES packets, AU split at PES carrying a PTS."""
import struct
from dataclasses import dataclass, field

PACK = b"\x00\x00\x01\xBA"
SKIP_IDS = {0xBB, 0xBC, 0xBE, 0xBF}


@dataclass
class PsFrame:
    offset: int
    length: int = 0
    pts: int | None = None
    es: list = field(default_factory=list)   # [(offset, length)]

    def keyframe(self, buf, codec: str = "h264") -> bool:
        from .codec import has_keyframe
        return any(has_keyframe(bytes(buf[off:off + min(ln, 4096)]), codec) for off, ln in self.es)


def sniff_codec(buf, start: int, end: int, budget: int = 1 << 20) -> str:
    """Codec of the elementary video inside a program stream, from its first parameter sets."""
    from .codec import sniff
    es, n = [], 0
    for f in walk(buf, start, end):
        for off, ln in f.es:
            es.append(bytes(buf[off:off + ln]))
            n += ln
        if n >= budget:
            break
    return sniff(b"".join(es))


def _pts(b) -> int:
    return (((b[0] >> 1) & 7) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | (b[3] << 7) | (b[4] >> 1)


def walk(buf, start: int, end: int):
    """Yield PsFrames for each pack in [start, end). Resyncs on the next pack header."""
    i = buf.find(PACK, start, end)
    cur = None
    while 0 <= i < end:
        if i + 14 > end or (buf[i + 4] & 0xC4) != 0x44:
            i = buf.find(PACK, i + 4, end)
            continue
        j = i + 14 + (buf[i + 13] & 7)
        frame = PsFrame(i)
        ok = True
        while j + 6 <= end and buf[j:j + 3] == b"\x00\x00\x01":
            sid = buf[j + 3]
            if sid == 0xBA:
                break
            ln = struct.unpack_from(">H", buf, j + 4)[0]
            if j + 6 + ln > end:
                ok = False
                break
            if 0xE0 <= sid <= 0xEF and ln >= 3:
                hl = buf[j + 8]
                if buf[j + 7] & 0x80 and hl >= 5:
                    pts = _pts(buf[j + 9:j + 14])
                    if frame.es and frame.pts is not None:   # second AU inside one pack
                        frame.length = j - frame.offset
                        yield frame
                        frame = PsFrame(j)
                    frame.pts = pts
                p0 = j + 9 + hl
                if p0 < j + 6 + ln:
                    frame.es.append((p0, j + 6 + ln - p0))
            elif sid not in SKIP_IDS and not (0xC0 <= sid <= 0xDF) and sid != 0xBD:
                break
            j += 6 + ln
        frame.length = j - frame.offset
        if ok and frame.es:
            yield frame
        nxt = j if (j + 4 <= end and buf[j:j + 4] == PACK) else buf.find(PACK, j, end)
        i = nxt
