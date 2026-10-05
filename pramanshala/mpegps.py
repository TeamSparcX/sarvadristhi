"""Minimal MPEG-2 Program Stream packer for H.264 (stream type 0x1B, stream id 0xE0).

Each access unit becomes: pack header, [PSM before keyframes], one or more PES packets.
Real Hikvision streams prefix a vendor 'IMKH' header; pramanshala omits it because its
internal layout is not verified. FFmpeg's mpegps demuxer reads the result directly.
"""
import struct

CLOCK = 90_000
MUX_RATE = 50_000  # units of 50 bytes/s
MAX_PES_PAYLOAD = 65535 - 3 - 5


def _crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
            crc &= 0xFFFFFFFF
    return crc


def pack_header(scr: int) -> bytes:
    b = bytearray(10)
    b[0] = 0x44 | ((scr >> 27) & 0x38) | ((scr >> 28) & 0x03)
    b[1] = (scr >> 20) & 0xFF
    b[2] = 0x04 | ((scr >> 12) & 0xF8) | ((scr >> 13) & 0x03)
    b[3] = (scr >> 5) & 0xFF
    b[4] = 0x04 | ((scr << 3) & 0xF8)  # SCR extension = 0
    b[5] = 0x01
    b[6] = (MUX_RATE >> 14) & 0xFF
    b[7] = (MUX_RATE >> 6) & 0xFF
    b[8] = ((MUX_RATE << 2) & 0xFC) | 0x03
    b[9] = 0xF8  # reserved, stuffing length 0
    return b"\x00\x00\x01\xBA" + bytes(b)


def psm(stream_type: int = 0x1B) -> bytes:
    """0x1B = H.264, 0x24 = H.265 (ISO/IEC 13818-1 stream types)."""
    body = bytes([0x80, 0x01]) + struct.pack(">H", 0) + struct.pack(">H", 4) \
        + bytes([stream_type, 0xE0]) + struct.pack(">H", 0)
    pre = b"\x00\x00\x01\xBC" + struct.pack(">H", len(body) + 4) + body
    return pre + struct.pack(">I", _crc32_mpeg(pre))


def _pts_bytes(pts: int) -> bytes:
    return bytes([0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF,
                  0x01 | ((pts >> 14) & 0xFE), (pts >> 7) & 0xFF,
                  0x01 | ((pts << 1) & 0xFE)])


def pes_packets(payload: bytes, pts: int) -> bytes:
    out, first = bytearray(), True
    for k in range(0, len(payload), MAX_PES_PAYLOAD):
        chunk = payload[k:k + MAX_PES_PAYLOAD]
        hdr = bytes([0x84, 0x80, 5]) + _pts_bytes(pts) if first else bytes([0x80, 0x00, 0])
        out += b"\x00\x00\x01\xE0" + struct.pack(">H", len(hdr) + len(chunk)) + hdr + chunk
        first = False
    return bytes(out)


def wrap_access_unit(au: bytes, pts_90k: int, keyframe: bool, stream_type: int = 0x1B) -> bytes:
    return pack_header(pts_90k) + (psm(stream_type) if keyframe else b"") + pes_packets(au, pts_90k)
