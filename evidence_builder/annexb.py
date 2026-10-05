"""H.264 Annex-B helpers: split an elementary stream into access units."""
from dataclasses import dataclass


@dataclass
class AccessUnit:
    data: bytes
    keyframe: bool  # contains an IDR slice (NAL type 5)


def nal_starts(buf: bytes):
    """Yield (startcode_pos, nal_pos) for every 3- or 4-byte start code."""
    i, n = 0, len(buf)
    while True:
        j = buf.find(b"\x00\x00\x01", i)
        if j < 0 or j + 3 >= n:
            return
        sc = j - 1 if j > 0 and buf[j - 1] == 0 else j
        yield sc, j + 3
        i = j + 3


def split_access_units(buf: bytes, codec: str = "h264") -> list[AccessUnit]:
    """Split on Access Unit Delimiters (H.264 NAL type 9, HEVC type 35). The stream must carry AUDs.
    Keyframe: an IDR slice (H.264 type 5) or an IRAP picture (HEVC types 16-23)."""
    hevc = codec == "hevc"
    starts = list(nal_starts(buf))
    aus, cur, key = [], None, False
    for k, (sc, nal) in enumerate(starts):
        nal_type = (buf[nal] >> 1) & 0x3F if hevc else buf[nal] & 0x1F
        if nal_type == (35 if hevc else 9):
            if cur is not None:
                aus.append(AccessUnit(buf[cur:sc], key))
            cur, key = sc, False
        elif (16 <= nal_type <= 23) if hevc else nal_type == 5:
            key = True
    if cur is not None:
        aus.append(AccessUnit(buf[cur:], key))
    return aus
