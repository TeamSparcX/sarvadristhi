"""Frame X-ray: frame-to-byte X-ray. While a recovered clip plays, the workbench shows where on
the preserved image each frame's bytes are and decodes the recorder's own header for that frame.

frames(): every frame of a clip with its container offset and length, from the item's frames.csv
          (recorder disks) or, for files recovered from removable media, the file's cluster runs.
header(): the first bytes of one frame, read-only from the original image, with the vendor fields
          (DHAV, Honeywell record, MPEG-PS pack/PES, Annex-B NAL) decoded and their byte positions."""
import csv
import json
import struct
from datetime import datetime, timezone
from pathlib import Path

from .capsule import resolve_source


def _case(case_dir: Path) -> dict:
    return json.loads((Path(case_dir) / "case.json").read_text(encoding="utf-8"))


def _item(c: dict, eid: str) -> dict:
    items = c.get("evidence_items") or [{"id": "E1", "path": c["evidence"]["source"], "dir": "", "bytes": c["evidence"]["bytes"],
                                         "family": c["detection"]["family"], "kind": c["case"].get("kind", "recorder-disk"),
                                         "filesystem": c.get("filesystem")}]
    return next(x for x in items if x["id"] == eid)


def frames(case_dir: Path, clip_id: str) -> dict:
    case_dir = Path(case_dir)
    c = _case(case_dir)
    k = next(x for x in c["clips"] if x["id"] == clip_id)
    eid = k.get("evidence", "E1")
    it = _item(c, eid)
    out = {"clip": clip_id, "evidence": eid, "image_bytes": it.get("bytes"), "family": it.get("family"),
           "camera": k.get("label"), "start_ms": k.get("start_ms"), "fps": k.get("fps"), "mode": None, "frames": [], "runs": []}
    fc = case_dir / (it.get("dir") or "") / "frames.csv"
    ch = k.get("local_channel", k.get("channel"))
    if fc.exists() and k.get("start_ms") is not None and it.get("kind") != "video-evidence":
        rows = []
        with open(fc, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if not r["time_ms"] or (r["channel"] and ch is not None and int(r["channel"]) != ch):
                    continue
                t = int(r["time_ms"])
                if k["start_ms"] <= t <= k["end_ms"]:
                    rows.append((t - k["start_ms"], int(r["offset"]), int(r["length"]), int(r["keyframe"]), r["evidence_level"]))
        rows.sort()
        out.update(mode="frames", frames=rows)
        return out
    fs = it.get("filesystem")
    if fs and k.get("source_file"):
        cs, base = fs["cluster_size"], fs["offset"]
        d = next((x for x in fs.get("deleted", []) if x["name"] == k["source_file"]), None)
        runs = (d or {}).get("clusters", {}).get("runs") if d else None
        if runs is None:
            f = next((x for x in fs["files"] if x["name"] == k["source_file"] or k["source_file"].startswith(str(x.get("inode")) + "_")), None)
            p = resolve_source(it["path"])
            if f and f.get("inode") and p:
                from .fsrecover import Volume
                try:
                    runs = Volume(str(p)).runs(f["inode"])
                except OSError:
                    runs = None
        if runs:
            out.update(mode="runs", runs=[[base + a * cs, n * cs] for a, n in runs], size=k.get("bytes"))
    return out


def _nal(b: bytes, i: int) -> dict | None:
    j = b.find(b"\x00\x00\x01", i)
    if j < 0 or j + 4 > len(b):
        return None
    h = b[j + 3]
    t264 = h & 0x1F
    names = {1: "non-IDR slice", 5: "IDR slice (keyframe)", 6: "SEI", 7: "SPS", 8: "PPS", 9: "access unit delimiter"}
    if j + 4 < len(b) and b[j + 4] == 0x01 and ((h >> 1) & 0x3F) in (19, 20, 21, 32, 33, 34, 35, 1, 0):
        t = (h >> 1) & 0x3F
        hn = {19: "IDR_W_RADL (keyframe)", 20: "IDR_N_LP (keyframe)", 21: "CRA", 32: "VPS", 33: "SPS", 34: "PPS",
              35: "access unit delimiter", 1: "TRAIL_R slice", 0: "TRAIL_N slice"}
        return {"name": "first NAL (H.265)", "value": f"type {t}: {hn.get(t, '')}", "at": j, "len": 5}
    return {"name": "first NAL (H.264)", "value": f"type {t264}: {names.get(t264, '')}", "at": j, "len": 4}


def decode(b: bytes, family: str) -> list[dict]:
    f = []
    if b[:4] == b"DHAV" and len(b) >= 24:
        from .formats.dhav import _date
        typ, ch = b[4], b[6]
        fno, total, date = struct.unpack_from("<III", b, 8)
        ds = _date(date)
        f += [{"name": "DHAV magic", "value": "DHAV", "at": 0, "len": 4},
              {"name": "frame type", "value": {0xFD: "0xFD video keyframe", 0xFC: "0xFC video delta", 0xF0: "0xF0 audio"}.get(typ, hex(typ)), "at": 4, "len": 1},
              {"name": "channel", "value": f"{ch + 1} (stored {ch})", "at": 6, "len": 1},
              {"name": "frame number", "value": f"{fno:,}", "at": 8, "len": 4},
              {"name": "frame length", "value": f"{total:,} bytes", "at": 12, "len": 4},
              {"name": "recorder date", "value": datetime.fromtimestamp(ds, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ds else "invalid",
               "at": 16, "len": 4},
              {"name": "millisecond counter", "value": str(struct.unpack_from("<H", b, 20)[0]), "at": 20, "len": 2}]
        n = _nal(b, 24)
    elif b[:1] in (b"\x82", b"\x02") and b[1:4] == b"\x80\x01\x00" and len(b) >= 20:
        w, h, n_, t = struct.unpack_from("<HHIQ", b, 4)
        f += [{"name": "record type", "value": "0x82 IDR frame" if b[0] == 0x82 else "0x02 non-IDR frame", "at": 0, "len": 1},
              {"name": "fixed bytes", "value": "80 01 00", "at": 1, "len": 3},
              {"name": "resolution", "value": f"{w} x {h}", "at": 4, "len": 4},
              {"name": "data length", "value": f"{n_:,} bytes", "at": 8, "len": 4},
              {"name": "recorder time (µs)", "value": datetime.fromtimestamp(t / 1e6, timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:23], "at": 12, "len": 8}]
        n = _nal(b, 20)
    elif b[:4] == b"\x00\x00\x01\xBA":
        f.append({"name": "MPEG-PS pack header", "value": "00 00 01 BA", "at": 0, "len": 4})
        i = 14 + (b[13] & 7) if len(b) > 14 else 14
        pstart = None
        while i + 6 <= len(b) and b[i:i + 3] == b"\x00\x00\x01":
            sid = b[i + 3]
            ln = struct.unpack_from(">H", b, i + 4)[0]
            if sid == 0xBC:
                st = b[i + 12] if i + 12 < len(b) else 0
                f.append({"name": "program stream map", "value": f"stream type 0x{st:02X} ({ {0x1B: 'H.264', 0x24: 'H.265'}.get(st, '?') })", "at": i, "len": 4})
            elif 0xE0 <= sid <= 0xEF:
                f.append({"name": "PES video stream", "value": f"0x{sid:02X}, {ln:,} bytes", "at": i, "len": 6})
                if i + 14 <= len(b) and b[i + 7] & 0x80:
                    p = b[i + 9:i + 14]
                    pts = (((p[0] >> 1) & 7) << 30) | (p[1] << 22) | ((p[2] >> 1) << 15) | (p[3] << 7) | (p[4] >> 1)
                    f.append({"name": "PTS (90 kHz)", "value": f"{pts:,} = {pts / 90000:.3f} s", "at": i + 9, "len": 5})
                pstart = i + 9 + (b[i + 8] if i + 8 < len(b) else 0)
                break
            i += 6 + ln
        n = _nal(b, pstart if pstart is not None else i)
    else:
        n = _nal(b, 0)
    if n:
        f.append(n)
    return f


def header(case_dir: Path, eid: str, offset: int, length: int = 96) -> dict:
    c = _case(case_dir)
    it = _item(c, eid)
    p = resolve_source(it["path"])
    if not p:
        return {"error": f"source image not found on this machine: {it['path']}"}
    offset = max(0, min(int(offset), (it.get("bytes") or 1) - 1))
    with open(p, "rb") as fh:
        fh.seek(offset)
        b = fh.read(length)
    return {"evidence": eid, "offset": offset, "hex": b.hex(), "fields": decode(b, it.get("family", "")),
            "source": it["path"], "read_only": True}
