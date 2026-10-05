"""Hikvision DVR/NVR filesystem: master sector, system logs, HIKBTREE index, OFNI IDR tables.

Structure offsets follow Han, Jeong & Lee (ICDF2C 2015) as refined on a real DVR
(firmware HIK.2011.03.08) by akira7799/hikvision-dvr-parser (MIT). Video in data blocks is
H.264 or H.265 in MPEG-2 Program Stream. Recovery does not trust the index: every data block is
walked, and per-block OFNI tables (which survive an index reset) supply GOP timestamps."""
import struct
from datetime import datetime, timezone

from ..formats import mpegps
from ..models import EvidenceLevel, Finding, Frame, Recovery
from .base import Plugin

SIG = b"HIKVISION@HANGZHOU"
LOG_SIG = b"RATS\x14\x00\x00\x00"
PROBE_OFFSETS = (0x200, 0x400, 0x800, 0x0)
PAGE, PAGE_HDR, ENTRY, OFNI = 4096, 0x60, 48, 56
NO_TIME = 0x7FFFFFFF
LOG_TYPES = {1: "motion", 2: "alarm/login/hdd-full", 3: "power/login/shutdown", 4: "hdd/recording"}
FORMAT_WORDS = ("initial", "format")


def _utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class HikvisionPlugin(Plugin):
    name = "hikvision"
    family = "hikvision"

    # ------------------------------------------------------------------ detection
    def _partition_offset(self, src) -> int:
        mbr = src.read(0, 512)
        if len(mbr) == 512 and mbr[510:512] == b"\x55\xAA" and mbr[0x1BE + 4]:
            return struct.unpack_from("<I", mbr, 0x1BE + 8)[0] * 512
        return 0

    def _find_master(self, src):
        base = self._partition_offset(src)
        for off in PROBE_OFFSETS:
            if src.read(base + off, len(SIG)) == SIG:
                return base, base + off
        i = src.buf.find(SIG, 0, min(src.size, 64 * 1024 * 1024))
        return (0, i) if i >= 0 else (base, None)

    def detect(self, src):
        base, ms = self._find_master(src)
        if ms is None:
            return 0.0, {}
        return 0.99, {"signature": SIG.decode(), "master_sector_offset": ms, "partition_offset": base}

    # ------------------------------------------------------------------ structures
    def master(self, src, base, ms) -> dict:
        d = src.read(ms, 256)
        q = lambda o: struct.unpack_from("<Q", d, o)[0]
        return {"offset": ms, "partition_offset": base, "capacity": q(0x38), "logs_offset": q(0x50),
                "logs_size": q(0x58), "video_offset": q(0x68), "block_size": q(0x78),
                "total_blocks": q(0x80), "btree1_offset": q(0x88), "btree1_size": q(0x90),
                "btree2_offset": q(0x98), "btree2_size": q(0xA0),
                "init_time": struct.unpack_from("<I", d, 0xE0)[0]}

    def logs(self, src, m) -> list[dict]:
        a = m["partition_offset"] + m["logs_offset"]
        end = a + m["logs_size"]
        out, i = [], src.buf.find(LOG_SIG, a, end)
        while i >= 0:
            ts, typ = struct.unpack_from("<IH", src.buf, i + 8)
            j = i + 14
            k = src.buf.find(b"\x00", j, min(j + 256, end))
            text = bytes(src.buf[j:k if k > 0 else j + 256]).decode("utf-8", "replace").strip()
            out.append({"offset": i, "time": ts, "time_utc": _utc(ts), "type": typ,
                        "type_name": LOG_TYPES.get(typ, f"0x{typ:02X}"), "text": text})
            i = src.buf.find(LOG_SIG, i + 14, end)
        return out

    def index(self, src, m, which: int) -> list[dict]:
        base = m["partition_offset"] + m[f"btree{which}_offset"]
        if not m[f"btree{which}_offset"]:
            return []
        hdr = src.read(base, 0x60)
        if hdr[:8] != b"HIKBTREE":
            if hdr[16:24] == b"HIKBTREE":
                base += 16
                hdr = src.read(base, 0x60)
            else:
                return []
        page_list = m["partition_offset"] + struct.unpack_from("<Q", hdr, 0x40)[0]
        n_pages = struct.unpack_from("<I", src.read(page_list, 4), 0)[0]
        entries = []
        for p in range(min(n_pages, 100000)):
            e = src.read(page_list + 0x60 + p * ENTRY, ENTRY)
            page = m["partition_offset"] + struct.unpack_from("<Q", e, 0)[0]
            data = src.read(page, PAGE)
            for s in range(PAGE_HDR, PAGE - ENTRY + 1, ENTRY):
                r = data[s:s + ENTRY]
                if r[8] != 0x00:
                    continue
                st, en = struct.unpack_from("<II", r, 24)
                entries.append({"channel": r[17], "start": st, "end": en,
                                "block_offset": struct.unpack_from("<Q", r, 32)[0], "partial": NO_TIME in (st, en)})
        return entries

    def ofni(self, src, block_abs: int, block_size: int) -> list[dict]:
        recs, pos = [], block_abs + block_size - OFNI
        while pos > block_abs and src.read(pos, 4) == b"OFNI":
            d = src.read(pos, OFNI)
            recs.append({"index": struct.unpack_from("<I", d, 4)[0], "channel": d[8],
                         "time": struct.unpack_from("<I", d, 12)[0],
                         "offset": struct.unpack_from("<I", d, 16)[0], "pos": pos})
            pos -= OFNI
        return recs

    # ------------------------------------------------------------------ recovery
    def recover(self, src) -> Recovery:
        base, ms = self._find_master(src)
        rec = Recovery(self.family, self.name)
        if ms is None:
            return rec
        m = self.master(src, base, ms)
        logs = self.logs(src, m)
        idx1, idx2 = self.index(src, m, 1), self.index(src, m, 2)
        if idx1 != idx2:
            rec.findings.append(Finding("index-mismatch", "warning",
                "Primary and backup HIKBTREE differ", {"primary": len(idx1), "backup": len(idx2)}))
        index = idx1 or idx2
        by_block = {e["block_offset"]: e for e in index}
        rec.metadata = {"master_sector": m, "init_time_utc": _utc(m["init_time"]),
                        "logs": logs, "index_entries": index}
        bs = m["block_size"]
        v0 = base + m["video_offset"]
        codec = mpegps.sniff_codec(src.buf, v0, min(src.size, v0 + 4 * bs))
        rec.metadata["codec"] = codec
        orphan_blocks, frames = 0, []
        for b in range(m["total_blocks"]):
            rel = m["video_offset"] + b * bs
            a = base + rel
            if a + bs > src.size:
                break
            ofni = self.ofni(src, a, bs)
            table_start = min((r["pos"] for r in ofni), default=a + bs)
            gop_time = {}
            for r in ofni:                  # scanned from the block end: newest table wins
                gop_time.setdefault(r["offset"], r)
            entry = by_block.get(rel)
            got = list(mpegps.walk(src.buf, a, table_start))
            if not got:
                continue
            if entry is None:
                orphan_blocks += 1
            gop, prev_end = None, None
            for f in got:
                key = f.keyframe(src.buf, codec)
                if prev_end is not None and f.offset != prev_end:
                    gop = None                      # byte discontinuity: GOP context is lost
                if gop and (f.pts is None or not 0 <= (f.pts - gop[1]) % (1 << 33) <= 60 * 90000):
                    gop = None                      # implausible PTS jump
                prev_end = f.offset + f.length
                r = gop_time.get(f.offset - a) if key else None
                if key:
                    gop = (r, f.pts) if r else None
                t_ms, chan, level, src_t = None, None, EvidenceLevel.CONTENT_ONLY, "none"
                if gop and f.pts is not None:
                    t_ms = gop[0]["time"] * 1000 + ((f.pts - gop[1]) % (1 << 33)) // 90
                    chan, level, src_t = gop[0]["channel"], EvidenceLevel.CARVED_TIMESTAMPED, "ofni"
                if entry and t_ms is not None and entry["start"] * 1000 <= t_ms < (entry["end"] + 1) * 1000 \
                        and entry["channel"] == chan:
                    level, src_t = EvidenceLevel.INDEX_CONFIRMED, "index"
                frames.append(Frame(f.offset, f.length, tuple(f.es), key, self.family, level,
                                    chan, t_ms, src_t, f.pts))
        rec.frames = frames
        self._findings(rec, m, logs, index, orphan_blocks)
        return rec

    def _findings(self, rec, m, logs, index, orphan_blocks):
        fmt = [l for l in logs if any(w in l["text"].lower() for w in FORMAT_WORDS)]
        for l in fmt:
            rec.findings.append(Finding("format-event", "critical",
                f"System log records a disk initialise/format at {l['time_utc']}", l))
        unindexed = [f for f in rec.frames if f.level < EvidenceLevel.INDEX_CONFIRMED]
        if orphan_blocks and unindexed:
            older = [f for f in unindexed if f.time_ms is not None and f.time_ms < m["init_time"] * 1000]
            rec.findings.append(Finding("index-reset", "critical",
                f"{orphan_blocks} data block(s) hold video that the live index does not list; "
                f"{len(older)} recovered frames predate the filesystem initialisation time "
                f"{_utc(m['init_time'])}", {"orphan_blocks": orphan_blocks,
                                            "frames_not_in_index": len(unindexed),
                                            "frames_before_init": len(older)}))
        if not index and rec.frames:
            rec.findings.append(Finding("empty-index", "warning",
                "The HIKBTREE index lists no recordings, yet video is present on disk", {}))
