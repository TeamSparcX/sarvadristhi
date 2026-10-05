"""Honeywell NVR video records, as published by Yoon & Hwang, "Forensic analysis of video data
deletion and recovery in Honeywell surveillance file system", DFRWS USA 2026 (arXiv 2605.07430),
section 5.4.6 and Figure 8, read in full on 2026-10-05:

    a 20-byte Custom Header precedes each frame's NAL data (little-endian):
      [0]      frame type: 0x82 = IDR frame, 0x02 = non-IDR frame
      [1:4]    fixed 80 01 00
      [4:6]    width   [6:8] height
      [8:12]   length of the NAL data that follows
      [12:20]  Unix time in microseconds
    then the Annex-B start code and NAL data. Each channel's data ends with 20 zero bytes
    ("End of Channel Data"); a dummy value fills the gap to the rounded length.

Known answer from the paper: timestamp 0x000644865C1BCEE6 = 2025-11-26 21:48:41.896 (UTC).
The Video Block List, Video Channel List and Record State areas are described in the paper but not
parsed here: records are found by structure, so formatting (which erases those lists) does not stop
recovery, but the recorder channel number is not known from the record itself."""
import re
import struct
import time
from dataclasses import dataclass

HDR = 20
FIXED = b"\x80\x01\x00"
TYPES = {0x82: True, 0x02: False}
MAX_LEN = 16 * 1024 * 1024
HEADER_RE = re.compile(rb"[\x82\x02]\x80\x01\x00")
_T_MIN = 946684800 * 1_000_000          # 2000-01-01


@dataclass
class Record:
    offset: int
    keyframe: bool
    width: int
    height: int
    length: int           # NAL data length after the header
    time_us: int

    @property
    def total(self) -> int:
        return HDR + self.length

    @property
    def time_ms(self) -> int:
        return self.time_us // 1000


def parse_at(buf, pos: int, end: int) -> Record | None:
    if pos + HDR + 4 > end or buf[pos] not in TYPES or bytes(buf[pos + 1:pos + 4]) != FIXED:
        return None
    w, h, n, t = struct.unpack_from("<HHIQ", buf, pos + 4)
    if not (64 <= w <= 8192 and 64 <= h <= 8192) or not (4 < n <= MAX_LEN) or pos + HDR + n > end:
        return None
    if not (_T_MIN <= t <= (time.time() + 86400) * 1_000_000):
        return None
    body = bytes(buf[pos + HDR:pos + HDR + 4])
    if body != b"\x00\x00\x00\x01" and body[:3] != b"\x00\x00\x01":
        return None
    return Record(pos, TYPES[buf[pos]], w, h, n, t)


def _boundary_ok(buf, pos: int, end: int) -> bool:
    """A record must be followed by another record header, the 20-byte End of Channel Data, or the end."""
    if pos >= end - 4:
        return True
    return bool(HEADER_RE.match(buf, pos, min(end, pos + 4))) or bytes(buf[pos:min(end, pos + HDR)]) == b"\x00" * min(HDR, end - pos)


def scan(buf, start: int = 0, end: int | None = None, rejects: list | None = None):
    """Every structurally valid record, by search (never by offset), skipping each record's body.
    A header whose declared length does not land on a record boundary is a torn record (for
    example cut by an overwrite); it is reported in `rejects`, and its body is not skipped."""
    end = len(buf) if end is None else end
    i = start
    while i < end:
        m = HEADER_RE.search(buf, i, end)
        if not m:
            return
        r = parse_at(buf, m.start(), end)
        if r and _boundary_ok(buf, r.offset + r.total, end):
            yield r
            i = r.offset + r.total
        else:
            if r and rejects is not None:
                rejects.append(r.offset)
            i = m.start() + 1
