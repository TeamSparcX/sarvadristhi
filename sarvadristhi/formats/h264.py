"""Raw H.264 Annex-B access-unit carving (vendor-agnostic).

Emulation prevention guarantees 00 00 00 never occurs inside a NAL unit, so a run of four
zero bytes reliably marks the end of coded data (padding, zeroed or unallocated space)."""
from dataclasses import dataclass

VCL = {1, 5}
ZERO4 = b"\x00\x00\x00\x00"


@dataclass
class AU:
    offset: int
    length: int
    keyframe: bool


def nal_positions(buf, start: int, end: int):
    i = start
    while True:
        j = buf.find(b"\x00\x00\x01", i, end)
        if j < 0 or j + 3 >= end:
            return
        yield (j - 1 if j > start and buf[j - 1] == 0 else j), j + 3
        i = j + 3


def carve_aus(buf, start: int, end: int):
    """A new AU starts at an AUD, at SPS/PPS/SEI following a slice, or at a slice with
    first_mb_in_slice == 0 following a slice. AUs without a slice are dropped."""
    au = None  # [start, has_vcl, keyframe, last_nal_pos]

    def close(a, limit):
        z = buf.find(ZERO4, a[3], limit)
        return AU(a[0], (limit if z < 0 else z) - a[0], a[2])

    for sc, nal in nal_positions(buf, start, end):
        b0 = buf[nal]
        t = b0 & 0x1F
        if b0 & 0x80 or t == 0 or t > 23:
            continue
        if au is not None and buf.find(ZERO4, au[3], sc) >= 0:   # coded data ended
            if au[1]:
                yield close(au, sc)
            au = None
        new = au is not None and au[1] and (
            t == 9 or t in (6, 7, 8) or (t in VCL and nal + 1 < end and buf[nal + 1] & 0x80))
        if au is None or new or t == 9:
            if au is not None and au[1]:
                yield close(au, sc)
            au = [sc, False, False, nal]
        au[3] = nal
        if t in VCL:
            au[1] = True
            au[2] |= t == 5
    if au is not None and au[1]:
        yield close(au, end)
