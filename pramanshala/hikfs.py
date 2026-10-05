"""Hikvision-layout disk image writer.

Structures and field offsets follow akira7799/hikvision-dvr-parser (MIT), which was derived
from a real DVR (firmware string HIK.2011.03.08) and Han, Jeong & Lee (ICDF2C 2015):
  Master Sector 'HIKVISION@HANGZHOU' at 0x200; system logs 'RATS'; HIKBTREE header,
  page list and 4 KiB pages of 48-byte data-block entries; per-block 'OFNI' IDR tables.
Fields the parser marks 'unknown' are written as zero. The SQLite recording database some
firmware keeps is NOT emulated.
"""
import struct
from dataclasses import dataclass, field

MS_OFFSET = 0x200
SIG = b"HIKVISION@HANGZHOU"
LOG_SIG = b"RATS\x14\x00\x00\x00"
PAGE_SIZE, PAGE_HDR, ENTRY = 4096, 0x60, 48
ENTRIES_PER_PAGE = (PAGE_SIZE - PAGE_HDR) // ENTRY  # 83
OFNI_REC = 56
NO_TIME = 0x7FFFFFFF
VERSION = b"HIK.2011.03.08"

LOG_MOTION, LOG_ALARM, LOG_POWER, LOG_HDD = 0x01, 0x02, 0x03, 0x04


@dataclass
class Layout:
    block_size: int = 4 * 1024 * 1024
    pages: int = 1
    logs_offset: int = 0x100000
    logs_size: int = 0x100000
    btree1_offset: int = 0x200000
    btree2_offset: int = 0x280000
    btree_size: int = 0x80000
    video_offset: int = 0x400000

    @property
    def total_blocks(self) -> int:
        return self.pages * ENTRIES_PER_PAGE

    @property
    def capacity(self) -> int:
        return self.video_offset + self.total_blocks * self.block_size

    def block_offset(self, idx: int) -> int:
        return self.video_offset + idx * self.block_size


@dataclass
class Block:
    channel: int = 0            # 1-based; 0 = never used
    start: int = NO_TIME
    end: int = NO_TIME
    data: bytearray = field(default_factory=bytearray)
    idrs: list = field(default_factory=list)   # (unix_ts, offset_in_block)

    def room(self, layout: Layout, extra_idr: int) -> int:
        return layout.block_size - len(self.data) - (len(self.idrs) + extra_idr) * OFNI_REC


def master_sector(layout: Layout, init_time: int) -> bytes:
    b = bytearray(256)
    b[0:len(SIG)] = SIG
    struct.pack_into("<Q", b, 0x38, layout.capacity)
    struct.pack_into("<Q", b, 0x50, layout.logs_offset)
    struct.pack_into("<Q", b, 0x58, layout.logs_size)
    struct.pack_into("<Q", b, 0x68, layout.video_offset)
    struct.pack_into("<Q", b, 0x78, layout.block_size)
    struct.pack_into("<Q", b, 0x80, layout.total_blocks)
    struct.pack_into("<Q", b, 0x88, layout.btree1_offset)
    struct.pack_into("<Q", b, 0x90, layout.btree_size)
    struct.pack_into("<Q", b, 0x98, layout.btree2_offset)
    struct.pack_into("<Q", b, 0xA0, layout.btree_size)
    struct.pack_into("<I", b, 0xE0, init_time)
    return bytes(b)


def system_logs(entries: list[tuple[int, int, str]]) -> bytes:
    out = bytearray()
    for ts, typ, text in sorted(entries):
        out += LOG_SIG + struct.pack("<IH", ts, typ) + text.encode() + b"\x00"
    return bytes(out)


def hikbtree(layout: Layout, base: int, blocks: list[Block], created: int) -> bytes:
    """Header at base, page list at base+0x1000, pages from base+0x2000 (offsets partition-relative)."""
    page_list = base + 0x1000
    page0 = base + 0x2000
    out = bytearray(0x2000 + layout.pages * PAGE_SIZE)
    out[0:8] = b"HIKBTREE"
    out[0x10:0x10 + len(VERSION)] = VERSION
    struct.pack_into("<I", out, 0x2C, created)
    struct.pack_into("<Q", out, 0x40, page_list)
    struct.pack_into("<Q", out, 0x48, page0)
    struct.pack_into("<I", out, 0x50, 0x60)
    struct.pack_into("<Q", out, 0x58, layout.total_blocks)
    struct.pack_into("<I", out, 0x1000, layout.pages)
    for p in range(layout.pages):
        e = 0x1000 + 0x60 + p * ENTRY
        struct.pack_into("<Q", out, e, page0 + p * PAGE_SIZE)
        struct.pack_into("<Q", out, e + 8, 0xFFFFFFFFFFFFFFFF)  # nonzero marker (field unknown)
    for idx in range(layout.total_blocks):
        blk = blocks[idx]
        p, slot = divmod(idx, ENTRIES_PER_PAGE)
        e = 0x2000 + p * PAGE_SIZE + PAGE_HDR + slot * ENTRY
        used = bool(blk.channel)
        out[e + 8:e + 16] = (b"\x00" if used else b"\xFF") * 8
        out[e + 17] = blk.channel if used else 0
        struct.pack_into("<II", out, e + 24, blk.start if used else NO_TIME,
                         blk.end if used else NO_TIME)
        struct.pack_into("<Q", out, e + 32, layout.block_offset(idx))
    return bytes(out)


def ofni_table(blk: Block) -> bytes:
    recs = bytearray()
    for k, (ts, off) in enumerate(blk.idrs):
        r = bytearray(OFNI_REC)
        r[0:4] = b"OFNI"
        struct.pack_into("<I", r, 4, k)
        r[8] = blk.channel
        struct.pack_into("<I", r, 12, ts)
        struct.pack_into("<I", r, 16, off)
        recs = r + recs        # stored from the end of the block backwards
    return bytes(recs)
