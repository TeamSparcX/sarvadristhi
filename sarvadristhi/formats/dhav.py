"""Dahua DHAV frame parsing with dual-signature (header + footer) validation.
Layout per FFmpeg libavformat/dhav.c."""
import struct
from calendar import timegm
from dataclasses import dataclass

VALID_TYPES = {0xFD, 0xFC, 0xF0, 0xF1}
VIDEO_TYPES = {0xFD, 0xFC}
HDR, FTR = 24, 8
MAX_FRAME = 8 * 1024 * 1024


@dataclass
class DhavFrame:
    offset: int
    length: int
    type: int
    channel0: int
    frame_no: int
    date_s: int          # recorder second, epoch (date treated as UTC)
    ts16: int            # millisecond counter mod 65536
    payload: tuple       # (offset, length)

    @property
    def keyframe(self) -> bool:
        return self.type == 0xFD

    @property
    def time_ms(self) -> int:
        base = self.date_s * 1000
        k = (self.ts16 - base) % 65536
        return base + k if k < 1000 else base


def _date(v: int) -> int | None:
    sec, mi, hr = v & 0x3F, (v >> 6) & 0x3F, (v >> 12) & 0x1F
    day, mon, yr = (v >> 17) & 0x1F, (v >> 22) & 0x0F, ((v >> 26) & 0x3F) + 2000
    if not (1 <= mon <= 12 and 1 <= day <= 31 and hr < 24 and mi < 60 and sec < 60):
        return None
    return timegm((yr, mon, day, hr, mi, sec, 0, 0, 0))


def parse_at(buf, pos: int, end: int):
    """(frame, None) if valid; (None, reason) if a DHAV header is present but invalid."""
    if pos + HDR > end or buf[pos + 4] not in VALID_TYPES:
        return None, None
    _, total, date = struct.unpack_from("<III", buf, pos + 8)
    if total < HDR + FTR or total > MAX_FRAME:
        return None, "bad-length"
    if pos + total > end:
        return None, "truncated"
    if buf[pos + total - 8:pos + total - 4] != b"dhav" or \
            struct.unpack_from("<I", buf, pos + total - 4)[0] != total:
        return None, "footer-mismatch"
    ds = _date(date)
    if ds is None:
        return None, "bad-date"
    p0 = pos + HDR + buf[pos + 22]
    return DhavFrame(pos, total, buf[pos + 4], buf[pos + 6],
                     struct.unpack_from("<I", buf, pos + 8)[0], ds,
                     struct.unpack_from("<H", buf, pos + 20)[0], (p0, pos + total - 8 - p0)), None


def scan(buf, start: int = 0, end: int | None = None, rejects: list | None = None):
    """Yield every validating DHAV frame; optionally collect (offset, reason) of torn ones."""
    end = len(buf) if end is None else end
    i = start
    while True:
        j = buf.find(b"DHAV", i, end)
        if j < 0:
            return
        f, why = parse_at(buf, j, end)
        if f:
            yield f
            i = j + f.length
        else:
            if why and rejects is not None:
                rejects.append((j, why))
            i = j + 1
