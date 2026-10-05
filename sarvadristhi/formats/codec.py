"""H.264 / H.265 (HEVC) awareness for every path that touches elementary video.

Most DVRs sold in India now default to H.265. The two codecs share Annex-B start codes but not
NAL headers: H.264 uses one byte (type = b & 0x1F), HEVC two (type = (b >> 1) & 0x3F, then a
layer/temporal byte that is 0x01 for single-layer streams). A byte that is an ordinary H.264
P-slice (0x41) reads as an HEVC VPS, so the codec of a stream is decided once, from parameter
sets, and then used consistently; it is never guessed NAL by NAL."""
from .h264 import AU, ZERO4, nal_positions

H264, HEVC = "h264", "hevc"
HEVC_PS = {32, 33, 34}                      # VPS, SPS, PPS
HEVC_IRAP = set(range(16, 24))              # BLA, IDR, CRA, reserved IRAP
HEVC_AUD, HEVC_PREFIX_SEI = 35, 39


def sniff(data, limit: int = 1 << 20, start: int = 0, end: int | None = None) -> str:
    """Decide the codec of an Annex-B stream from its parameter sets (H.264 SPS/PPS 0x67/0x68
    family vs HEVC VPS/SPS/PPS 0x40/0x42/0x44 followed by 0x01). Defaults to H.264.
    The window starts at the first start code, so leading zeros or headers do not hide the video."""
    end = len(data) if end is None else end
    j = data.find(b"\x00\x00\x01", start, end)
    data = bytes(data[j:min(end, j + limit)]) if j >= 0 else b""
    h264 = hevc = 0
    i = 0
    while True:
        j = data.find(b"\x00\x00\x01", i)
        if j < 0 or j + 5 > len(data):
            break
        b0, b1 = data[j + 3], data[j + 4]
        if b1 == 0x01 and b0 in (0x40, 0x42, 0x44):
            hevc += 1
        elif (b0 & 0x9F) in (0x07, 0x08) and b0 & 0x60:    # SPS/PPS with nal_ref_idc > 0
            h264 += 1
        i = j + 3
    return HEVC if hevc > h264 else H264


def nal_type(b0: int, codec: str) -> int:
    return (b0 >> 1) & 0x3F if codec == HEVC else b0 & 0x1F


def is_vcl(b0: int, codec: str) -> bool:
    t = nal_type(b0, codec)
    return t <= 31 if codec == HEVC else t in (1, 5)


def starts_gop(b0: int, codec: str) -> bool:
    """NAL types that mark a decodable entry point: parameter sets or an IRAP/IDR picture."""
    t = nal_type(b0, codec)
    return (t in HEVC_IRAP or t in HEVC_PS) if codec == HEVC else t in (5, 7)


def has_keyframe(data: bytes, codec: str) -> bool:
    i = 0
    while True:
        j = data.find(b"\x00\x00\x01", i)
        if j < 0 or j + 3 >= len(data):
            return False
        if starts_gop(data[j + 3], codec):
            return True
        i = j + 3


def ffmpeg_format(codec: str) -> str:
    return "hevc" if codec == HEVC else "h264"


def carve_aus_hevc(buf, start: int, end: int):
    """HEVC access units. A new AU starts at an AUD, at a VPS/SPS/PPS or prefix SEI following a
    picture, or at a slice with first_slice_segment_in_pic_flag set following a picture.
    Same end-of-data rule as H.264: emulation prevention forbids 00 00 00 inside a NAL unit."""
    au = None  # [start, has_vcl, keyframe, last_nal_pos]

    def close(a, limit):
        z = buf.find(ZERO4, a[3], limit)
        return AU(a[0], (limit if z < 0 else z) - a[0], a[2])

    for sc, nal in nal_positions(buf, start, end):
        if nal + 1 >= end:
            break
        b0, b1 = buf[nal], buf[nal + 1]
        if b0 & 0x80 or (b1 & 0x07) == 0:               # forbidden bit, temporal_id_plus1 == 0
            continue
        t = (b0 >> 1) & 0x3F
        if t > 40:
            continue
        if au is not None and buf.find(ZERO4, au[3], sc) >= 0:
            if au[1]:
                yield close(au, sc)
            au = None
        vcl = t <= 31
        new = au is not None and au[1] and (
            t == HEVC_AUD or t in HEVC_PS or t == HEVC_PREFIX_SEI or (vcl and nal + 2 < end and buf[nal + 2] & 0x80))
        if au is None or new or t == HEVC_AUD:
            if au is not None and au[1]:
                yield close(au, sc)
            au = [sc, False, False, nal]
        au[3] = nal
        if vcl:
            au[1] = True
            au[2] |= t in HEVC_IRAP
    if au is not None and au[1]:
        yield close(au, end)
