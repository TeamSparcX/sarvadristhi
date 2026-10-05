"""Dahua DHAV frame writer and validating reader.

Layout follows FFmpeg's DHAV demuxer (libavformat/dhav.c):
  0  'DHAV'                 4
  4  type  (0xFD I, 0xFC P, 0xF0 audio, 0xF1 aux)
  5  subtype
  6  channel (0-based)
  7  frame sub-number
  8  frame number           u32 LE
  12 frame length           u32 LE, whole frame incl. header and footer
  16 date                   u32 LE, packed sec|min<<6|hour<<12|day<<17|month<<22|(year-2000)<<26
  20 timestamp              u16 LE, millisecond counter (wraps)
  22 extension length       u8
  23 checksum               u8  (FFmpeg ignores it; pramanshala writes sum(bytes 0..22) & 0xFF, UNVERIFIED)
  24 extension records, then payload, then footer 'dhav' + u32 LE frame length.
"""
import struct
from dataclasses import dataclass
from datetime import datetime

TYPE_I, TYPE_P, TYPE_AUDIO, TYPE_AUX = 0xFD, 0xFC, 0xF0, 0xF1
VALID_TYPES = {TYPE_I, TYPE_P, TYPE_AUDIO, TYPE_AUX}
CODEC_H264 = 0x02
HEADER_LEN, FOOTER_LEN = 24, 8


def pack_date(dt: datetime) -> int:
    return (dt.second | dt.minute << 6 | dt.hour << 12 | dt.day << 17
            | dt.month << 22 | (dt.year - 2000) << 26)


def unpack_date(v: int) -> datetime:
    return datetime(((v >> 26) & 0x3F) + 2000, (v >> 22) & 0x0F, (v >> 17) & 0x1F,
                    (v >> 12) & 0x1F, (v >> 6) & 0x3F, v & 0x3F)


def build_frame(channel0: int, frame_no: int, when: datetime, ts_ms: int,
                payload: bytes, keyframe: bool, width: int, height: int, fps: int) -> bytes:
    ext = b""
    if keyframe:
        ext = bytes([0x82, 0, 0, 0]) + struct.pack("<HH", width, height) \
            + bytes([0x81, 0, CODEC_H264, fps])
    total = HEADER_LEN + len(ext) + len(payload) + FOOTER_LEN
    head = (b"DHAV" + bytes([TYPE_I if keyframe else TYPE_P, 0, channel0, 0])
            + struct.pack("<III", frame_no, total, pack_date(when))
            + struct.pack("<H", ts_ms & 0xFFFF) + bytes([len(ext)]))
    head += bytes([sum(head) & 0xFF])
    return head + ext + payload + b"dhav" + struct.pack("<I", total)


@dataclass
class Frame:
    offset: int
    length: int
    type: int
    channel0: int
    frame_no: int
    when: datetime
    ts_ms: int
    payload_offset: int
    payload_length: int


def parse_at(buf, pos: int, limit: int | None = None) -> Frame | None:
    """Return a Frame only if header AND matching footer validate (dual-signature check)."""
    end = len(buf) if limit is None else limit
    if pos + HEADER_LEN > end or buf[pos:pos + 4] != b"DHAV" or buf[pos + 4] not in VALID_TYPES:
        return None
    frame_no, total, date = struct.unpack_from("<III", buf, pos + 8)
    if total < HEADER_LEN + FOOTER_LEN or pos + total > end:
        return None
    if buf[pos + total - 8:pos + total - 4] != b"dhav" or \
            struct.unpack_from("<I", buf, pos + total - 4)[0] != total:
        return None
    try:
        when = unpack_date(date)
    except ValueError:
        return None
    ext_len = buf[pos + 22]
    po = pos + HEADER_LEN + ext_len
    return Frame(pos, total, buf[pos + 4], buf[pos + 6], frame_no, when,
                 struct.unpack_from("<H", buf, pos + 20)[0], po, pos + total - 8 - po)


def scan(buf, start: int = 0, end: int | None = None):
    """Carve every validating DHAV frame in buf[start:end]."""
    end = len(buf) if end is None else end
    i = start
    while True:
        j = buf.find(b"DHAV", i, end)
        if j < 0:
            return
        f = parse_at(buf, j, end)
        if f:
            yield f
            i = j + f.length
        else:
            i = j + 1
