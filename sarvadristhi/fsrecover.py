"""Deleted-file recovery from removable media (USB sticks, SD cards, export drives).

Recorders export footage to USB sticks formatted FAT32, exFAT or NTFS. This module reads an
image of such a stick through The Sleuth Kit (pytsk3), lists every file - allocated and
deleted - and recovers deleted files whose clusters have not been reused.

On NTFS it also parses the change journal ($Extend/$UsnJrnl:$J). Each journal record names a
file, the operation (create, write, rename, delete ...) and a timestamp, so a deletion can be
dated to the second. FAT and exFAT keep no such record; there the report bounds the deletion
("after the file's last modification") and says so.

Every recovered file is hashed (MD5 + SHA-256), checked against the clusters of live files
(reuse means the content may be someone else's), checked for a matching file signature, and,
for video, test-decoded with FFmpeg. Each check is reported with its result."""
import hashlib
import shutil
import struct
import subprocess
from datetime import datetime, timezone
from pathlib import Path

CHUNK = 4 * 1024 * 1024
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".dav", ".h264", ".264", ".h265", ".265", ".ps", ".mpg", ".ts"}

# USN_RECORD reason flags (winioctl.h)
USN_REASONS = {
    0x00000001: "DATA_OVERWRITE", 0x00000002: "DATA_EXTEND", 0x00000004: "DATA_TRUNCATION",
    0x00000010: "NAMED_DATA_OVERWRITE", 0x00000020: "NAMED_DATA_EXTEND", 0x00000040: "NAMED_DATA_TRUNCATION",
    0x00000100: "FILE_CREATE", 0x00000200: "FILE_DELETE", 0x00000400: "EA_CHANGE",
    0x00000800: "SECURITY_CHANGE", 0x00001000: "RENAME_OLD_NAME", 0x00002000: "RENAME_NEW_NAME",
    0x00004000: "INDEXABLE_CHANGE", 0x00008000: "BASIC_INFO_CHANGE", 0x00010000: "HARD_LINK_CHANGE",
    0x00020000: "COMPRESSION_CHANGE", 0x00040000: "ENCRYPTION_CHANGE", 0x00080000: "OBJECT_ID_CHANGE",
    0x00100000: "REPARSE_POINT_CHANGE", 0x00200000: "STREAM_CHANGE", 0x80000000: "CLOSE",
}

SIGNATURES = [  # (offset, bytes, type)
    (4, b"ftyp", "MP4/MOV (ISO BMFF)"), (0, b"RIFF", "RIFF (AVI/WAV)"), (0, b"\x1aE\xdf\xa3", "Matroska/WebM"),
    (0, b"DHAV", "Dahua DHAV"), (0, b"\x00\x00\x01\xba", "MPEG-PS"), (0, b"\x00\x00\x00\x01", "H.264/H.265 Annex B"),
    (0, b"\x47", "MPEG-TS"), (0, b"\xff\xd8\xff", "JPEG"), (0, b"\x89PNG", "PNG"), (0, b"%PDF", "PDF"),
    (0, b"PK\x03\x04", "ZIP/Office"),
]


def available() -> bool:
    try:
        import pytsk3  # noqa: F401
        return True
    except ImportError:
        return False


def _ts(v) -> str | None:
    return datetime.fromtimestamp(v, timezone.utc).isoformat(timespec="seconds") if v else None


def _filetime(ft: int) -> str | None:
    if not ft:
        return None
    return datetime.fromtimestamp((ft - 116444736000000000) / 1e7, timezone.utc).isoformat(timespec="milliseconds")


def _enum_name(module, prefix: str, value) -> str:
    """pytsk3 exposes enum members as ints; map one back to its constant name."""
    names = [n[len(prefix):] for n in dir(module) if n.startswith(prefix) and getattr(module, n) == value]
    names = [n for n in names if not n.endswith(("DETECT", "AUTO"))] or names
    return names[0] if names else str(value)


def _signature(head: bytes) -> str | None:
    for off, sig, name in SIGNATURES:
        if head[off:off + len(sig)] == sig:
            return name
    return None


class Volume:
    """One filesystem inside an image (partitioned disk or bare 'superfloppy' volume)."""

    def __init__(self, image: str):
        import pytsk3
        self.pytsk3 = pytsk3
        self.image = image
        self.img = pytsk3.Img_Info(image)
        self.partitions, self.offset, self.table = [], 0, "none (bare volume)"
        try:
            vs = pytsk3.Volume_Info(self.img)
            self.table = _enum_name(pytsk3, "TSK_VS_TYPE_", vs.info.vstype)
            for p in vs:
                desc = p.desc.decode("utf-8", "replace")
                self.partitions.append({"slot": p.addr, "start_sector": p.start, "sectors": p.len,
                                        "description": desc, "allocated": bool(p.flags & pytsk3.TSK_VS_PART_FLAG_ALLOC)})
            for p in self.partitions:
                if p["allocated"]:
                    try:
                        pytsk3.FS_Info(self.img, offset=p["start_sector"] * vs.info.block_size)
                        self.offset = p["start_sector"] * vs.info.block_size
                        break
                    except OSError:
                        continue
        except OSError:
            pass
        self.fs = pytsk3.FS_Info(self.img, offset=self.offset)
        self.fstype = _enum_name(pytsk3, "TSK_FS_TYPE_", self.fs.info.ftype)
        self.block_size = self.fs.info.block_size

    # ----------------------------------------------------------------- listing
    def walk(self, max_files: int = 20000) -> list[dict]:
        out, seen = [], set()
        tsk = self.pytsk3

        def visit(directory, parent: str, depth: int):
            for f in directory:
                if len(out) >= max_files:
                    return
                name = f.info.name.name.decode("utf-8", "replace")
                if name in (".", "..") or f.info.meta is None and not (f.info.name.flags & tsk.TSK_FS_NAME_FLAG_UNALLOC):
                    continue
                m = f.info.meta
                addr = int(m.addr) if m else int(f.info.name.meta_addr)
                if m is None and addr:
                    try:                # deleted name without loaded metadata: look the entry up
                        m = self.fs.open_meta(inode=addr).info.meta
                    except OSError:
                        m = None
                path = f"{parent}/{name}"
                name_deleted = bool(f.info.name.flags & tsk.TSK_FS_NAME_FLAG_UNALLOC)
                meta_deleted = bool(m and m.flags & tsk.TSK_FS_META_FLAG_UNALLOC)
                is_dir = bool(m and m.type == tsk.TSK_FS_META_TYPE_DIR)
                key = (addr, path)
                if key in seen:
                    continue
                seen.add(key)
                rec = {"path": path, "name": name, "dir": is_dir, "deleted": name_deleted or meta_deleted,
                       "inode": addr or None, "seq": int(m.seq) if m else None,
                       "size": int(m.size) if m else 0,
                       "created": _ts(m.crtime) if m else None, "modified": _ts(m.mtime) if m else None,
                       "accessed": _ts(m.atime) if m else None, "changed": _ts(m.ctime) if m else None,
                       "system": name.startswith("$") or parent.startswith("/$"),
                       # a deleted name whose metadata entry is live again belongs to another file now
                       "meta_reused": name_deleted and m is not None and not meta_deleted}
                out.append(rec)
                if is_dir and depth < 12 and name not in ("$OrphanFiles",) and not rec["meta_reused"]:
                    try:
                        visit(f.as_directory(), path, depth + 1)
                    except OSError:
                        pass
                elif is_dir and name == "$OrphanFiles":
                    try:
                        visit(f.as_directory(), path, depth + 1)
                    except OSError:
                        pass

        visit(self.fs.open_dir(path="/"), "", 0)
        return out

    def runs(self, inode: int) -> list[tuple[int, int]]:
        """Cluster runs of the default data attribute: [(first_block, n_blocks), ...]."""
        tsk = self.pytsk3
        f = self.fs.open_meta(inode=inode)
        out = []
        for attr in f:
            if attr.info.type not in (tsk.TSK_FS_ATTR_TYPE_NTFS_DATA, tsk.TSK_FS_ATTR_TYPE_DEFAULT) or attr.info.name:
                continue
            if not attr.info.flags & tsk.TSK_FS_ATTR_NONRES:
                return []           # resident data lives inside the MFT record itself
            for r in attr:
                if r.len and not r.flags & tsk.TSK_FS_ATTR_RUN_FLAG_SPARSE:
                    out.append((int(r.addr), int(r.len)))
        return out

    def read(self, inode: int, size: int, dst: Path) -> dict:
        f = self.fs.open_meta(inode=inode)
        md5, sha = hashlib.md5(), hashlib.sha256()
        head, n = b"", 0
        with open(dst, "xb") as out:
            while n < size:
                b = f.read_random(n, min(CHUNK, size - n))
                if not b:
                    break
                if not head:
                    head = b[:64]
                md5.update(b)
                sha.update(b)
                out.write(b)
                n += len(b)
        return {"bytes": n, "md5": md5.hexdigest(), "sha256": sha.hexdigest(), "head": head}

    # ----------------------------------------------------------------- $UsnJrnl
    def usn_journal(self, limit: int = 200000) -> dict:
        tsk = self.pytsk3
        if self.fstype != "NTFS":
            return {"present": False, "reason": f"{self.fstype} keeps no change journal"}
        try:
            f = self.fs.open("/$Extend/$UsnJrnl")
        except OSError:
            return {"present": False, "reason": "no $Extend/$UsnJrnl on this volume (journal never enabled)"}
        j = None
        for attr in f:
            if attr.info.name == b"$J":
                j = attr
        if j is None:
            return {"present": False, "reason": "$UsnJrnl has no $J stream"}
        size = j.info.size
        recs, off = [], 0
        # $J is sparse: old records are zeroed out ahead of the live region. Read in chunks.
        while off < size and len(recs) < limit:
            data = f.read_random(off, min(CHUNK, size - off), j.info.type, j.info.id)
            if not data:
                break
            i = 0
            while i + 60 <= len(data):
                ln = struct.unpack_from("<I", data, i)[0]
                if ln == 0:             # skip the zeroed run in one step
                    rest = data[i:]
                    i += max(8, (len(rest) - len(rest.lstrip(b"\x00"))) // 8 * 8)
                    continue
                if ln < 60 or ln > 4096 or i + ln > len(data):
                    if i + ln > len(data) and ln <= 4096:
                        break           # record straddles chunk boundary: re-read from here
                    i += 8
                    continue
                major = struct.unpack_from("<H", data, i + 4)[0]
                if major == 2:
                    fref, pref, usn, ts, reason = struct.unpack_from("<QQqQI", data, i + 8)
                    nl, no = struct.unpack_from("<HH", data, i + 56)
                    rec_no, seq = fref & 0xFFFFFFFFFFFF, fref >> 48
                    prec = pref & 0xFFFFFFFFFFFF
                elif major == 3:
                    fref = int.from_bytes(data[i + 8:i + 24], "little")
                    pref = int.from_bytes(data[i + 24:i + 40], "little")
                    usn, ts, reason = struct.unpack_from("<qQI", data, i + 40)
                    nl, no = struct.unpack_from("<HH", data, i + 72)
                    rec_no, seq, prec = fref & 0xFFFFFFFFFFFF, (fref >> 48) & 0xFFFF, pref & 0xFFFFFFFFFFFF
                else:
                    i += 8
                    continue
                name = data[i + no:i + no + nl].decode("utf-16-le", "replace")
                recs.append({"usn": usn, "time": _filetime(ts), "mft_record": rec_no, "seq": seq,
                             "parent_record": prec, "name": name, "reason_flags": reason,
                             "reasons": [v for k, v in USN_REASONS.items() if reason & k]})
                i += (ln + 7) // 8 * 8
            off += i if i else len(data)
        return {"present": True, "stream_bytes": size, "records": recs}


def _probe_video(path: Path) -> dict:
    if not shutil.which("ffprobe"):
        return {"checked": False}
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                        "stream=codec_name,width,height,nb_read_packets,avg_frame_rate:format=duration",
                        "-of", "json", str(path)], capture_output=True, text=True, encoding="utf-8")
    import json
    try:
        j = json.loads(r.stdout or "{}")
        s = (j.get("streams") or [{}])[0]
        dec = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
                             capture_output=True, text=True, encoding="utf-8")
        return {"checked": True, "codec": s.get("codec_name"), "width": s.get("width"), "height": s.get("height"),
                "packets": int(s.get("nb_read_packets") or 0), "duration_s": float(j.get("format", {}).get("duration") or 0),
                "decode_errors": len([l for l in dec.stderr.splitlines() if l.strip()]),
                "decodes_cleanly": r.returncode == 0 and dec.returncode == 0 and not dec.stderr.strip()}
    except (ValueError, KeyError):
        return {"checked": True, "decodes_cleanly": False, "error": r.stderr[-300:]}


def analyse(image: str, out_dir: Path, ledger=None, log=print) -> dict:
    """List, date and recover deleted files. Returns the 'filesystem' section of case.json."""
    out_dir = Path(out_dir)
    rec_dir = out_dir / "recovered"
    rec_dir.mkdir(parents=True, exist_ok=True)
    trace = []

    def step(name, detail, **data):
        trace.append({"step": name, "detail": detail, **data})
        log(f"  [fs] {name}: {detail}")

    v = Volume(image)
    step("volume", f"partition table {v.table}; filesystem {v.fstype} at byte offset {v.offset}; "
         f"cluster {v.block_size} bytes", partitions=v.partitions)
    files = v.walk()
    user_files = [f for f in files if not f["system"] and not f["dir"]]
    deleted = [f for f in user_files if f["deleted"]]
    step("walk", f"{len(files)} directory entries; {len(user_files)} user files; {len(deleted)} marked deleted")

    # clusters held by live files: a deleted file overlapping these has been (partly) overwritten
    live_blocks = []
    for f in files:
        if not f["deleted"] and f["inode"] is not None and f["size"] and not f["dir"]:
            try:
                live_blocks += v.runs(f["inode"])
            except OSError:
                pass
    step("allocation map", f"{len(live_blocks)} cluster runs belong to live files")

    usn = v.usn_journal()
    if usn["present"]:
        step("change journal", f"$UsnJrnl:$J parsed: {len(usn['records'])} records "
             f"({usn['records'][0]['time'] if usn['records'] else '-'} to {usn['records'][-1]['time'] if usn['records'] else '-'})")
    else:
        step("change journal", usn["reason"])

    results = []
    for f in deleted:
        r = {**f, "checks": []}
        # --- when was it deleted?
        ev = [x for x in usn.get("records", []) if x["mft_record"] == f["inode"] and
              (x["name"] == f["name"] or not f["name"])] if usn["present"] else []
        r["journal"] = ev[-40:]
        dels = [x for x in ev if "FILE_DELETE" in x["reasons"]]
        creates = [x for x in ev if "FILE_CREATE" in x["reasons"]]
        if dels:
            r["deleted_at"] = dels[-1]["time"]
            r["deleted_at_basis"] = f"$UsnJrnl record USN {dels[-1]['usn']}: FILE_DELETE of '{dels[-1]['name']}' (MFT record {f['inode']})"
        else:
            r["deleted_at"] = None
            r["deleted_at_basis"] = (f"no journal record; deleted after last modification {f['modified']}"
                                     if f["modified"] else "no journal record and no timestamps")
        if creates:
            r["written_at"] = creates[0]["time"]
        # --- can it be recovered intact?
        if f["meta_reused"] or f["inode"] is None:
            r["checks"].append({"check": "metadata entry", "ok": False,
                                "detail": "MFT/directory entry now belongs to another file; content not attributable"})
            r["recoverable"] = False
            results.append(r)
            continue
        try:
            runs = v.runs(f["inode"])
        except OSError as e:
            runs = []
            r["checks"].append({"check": "cluster map", "ok": False, "detail": str(e)})
        overlap = sum(max(0, min(a + n, b + m) - max(a, b)) for a, n in runs for b, m in live_blocks)
        total = sum(n for _, n in runs)
        r["clusters"] = {"runs": runs[:50], "count": total, "overwritten": overlap}
        r["checks"].append({"check": "cluster reuse", "ok": overlap == 0,
                            "detail": f"{overlap} of {total} clusters now belong to live files" if total
                            else "data is resident in the metadata record"})
        dst = rec_dir / f"{f['inode']}_{f['name']}"
        if dst.exists():
            dst.unlink()
        got = v.read(f["inode"], f["size"], dst)
        r["recovered_file"] = str(dst.relative_to(out_dir)).replace("\\", "/")
        r["hash"] = {"md5": got["md5"], "sha256": got["sha256"]}
        r["checks"].append({"check": "length", "ok": got["bytes"] == f["size"],
                            "detail": f"read {got['bytes']} of {f['size']} bytes"})
        sig = _signature(got["head"])
        ext = Path(f["name"]).suffix.lower()
        r["signature"] = sig
        r["checks"].append({"check": "file signature", "ok": sig is not None,
                            "detail": f"header identifies {sig}" if sig else "no known signature in header"})
        if ext in VIDEO_EXT or (sig and "MP4" in sig):
            p = _probe_video(dst)
            r["video"] = p
            if p.get("checked"):
                r["checks"].append({"check": "video decode", "ok": bool(p.get("decodes_cleanly")),
                                    "detail": f"{p.get('codec')} {p.get('width')}x{p.get('height')}, "
                                              f"{p.get('packets')} packets, {p.get('duration_s', 0):.1f} s, "
                                              f"{p.get('decode_errors', 0)} decoder errors"})
        r["recoverable"] = all(c["ok"] for c in r["checks"])
        r["verdict"] = ("RECOVERED INTACT" if r["recoverable"] else
                        "RECOVERED WITH DAMAGE" if got["bytes"] else "NOT RECOVERABLE")
        if ledger:
            ledger.append("file-recovered", path=f["path"], inode=f["inode"], deleted_at=r["deleted_at"],
                          output=r["recovered_file"], verdict=r["verdict"], **r["hash"])
        step("recover", f"{f['path']}: {r['verdict']}; deleted {r['deleted_at'] or 'time unknown'}")
        results.append(r)

    # journal timeline of user-visible activity (skip metadata-only noise)
    activity = [x for x in usn.get("records", []) if not x["name"].startswith("$") and
                x["reason_flags"] & (0x100 | 0x200 | 0x1000 | 0x2000 | 0x2)]
    return {"image": image, "partition_table": v.table, "partitions": v.partitions, "filesystem": v.fstype,
            "offset": v.offset, "cluster_size": v.block_size,
            "files": [f for f in files if not f["system"]][:2000],
            "live_runs": live_blocks[:400], "deleted": results, "journal": {"present": usn["present"], "reason": usn.get("reason"),
                                            "records": len(usn.get("records", [])), "activity": activity[-300:]},
            "trace": trace}


def is_filesystem(image: str) -> bool:
    """True if The Sleuth Kit finds a FAT/exFAT/NTFS/ext filesystem (i.e. not a DVR disk)."""
    if not available():
        return False
    try:
        Volume(image)
        return True
    except OSError:
        return False
