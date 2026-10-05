"""Byte-to-artifact replay capsule.

A capsule lists, for every recovered clip, the exact byte ranges of the preserved evidence image
it was built from, in order, plus the hash the analysis recorded. Anyone holding the capsule and
the image can rebuild each clip's video stream on another machine, without sarvadristhi's parsers, and
check it hash for hash. The capsule is sealed with the examiner workstation's Ed25519 key.

What "replay" means here (stated in every capsule): the recovered elementary stream (raw H.264)
or the recovered file is byte-identical. MP4 files are remuxed derivatives whose container bytes
depend on the FFmpeg version, so they are listed with their hash but not replayed."""
import hashlib
import json
import time
from pathlib import Path

from . import __version__, ed25519
from .custody import Ledger

FORMAT = "punarnirmiti/1"
DEFINITION = ("Replay = the clip's recovered elementary stream (or recovered file) is rebuilt from the listed "
              "byte ranges of the preserved image and its SHA-256 matches the hash recorded at analysis time. "
              "MP4 files are remuxed derivatives and are not replayed byte for byte.")


def resolve_source(path: str, near: Path | None = None) -> Path | None:
    """Find an evidence image recorded on another machine: as recorded, under this project's kit\\,
    or next to the capsule."""
    p = Path(path)
    root = Path(__file__).resolve().parents[1]
    tries = [p if p.is_absolute() else root / p]
    if "kit" in p.parts:
        tries.append(root.joinpath(*p.parts[p.parts.index("kit"):]))
    if near:
        tries.append(near / p.name)
    return next((t for t in tries if t.exists()), None)


def _merge(ranges):
    out = []
    for o, n in ranges:
        if out and out[-1][0] + out[-1][1] == o:
            out[-1][1] += n
        else:
            out.append([o, n])
    return out


def _hash_ranges(f, ranges, size_limit=None, flip=None) -> tuple[str, int]:
    h, total = hashlib.sha256(), 0
    for i, (o, n) in enumerate(ranges):
        f.seek(o)
        b = f.read(n)
        if size_limit is not None:
            b = b[:max(0, size_limit - total)]
        if flip is not None and i == 0 and len(b) > flip:     # simulated tampering, in memory only
            b = b[:flip] + bytes([b[flip] ^ 0xFF]) + b[flip + 1:]
        h.update(b)
        total += len(b)
    return h.hexdigest(), total


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def build(case_dir: Path, log=print) -> dict:
    """Re-run each item's parser on the preserved image, record every clip's byte ranges, and check
    that they rebuild the stream the analysis exported. Writes capsule/ into the case folder."""
    from .clips import build_clips
    from .recover import recover
    from .source import Source
    case_dir = Path(case_dir)
    c = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    items = c.get("evidence_items") or [{"id": "E1", "name": c["case"]["case_id"], "path": c["evidence"]["source"],
                                         "kind": c["case"].get("kind", "recorder-disk"), "hash": c["evidence"].get("before"),
                                         "recovery": c["recovery"], "filesystem": c.get("filesystem")}]
    multi = bool(c.get("evidence_items"))
    by_id = {k["id"]: k for k in c["clips"]}
    t0 = time.time()
    cap_items, cap_clips, problems = [], [], []
    for it in items:
        src_path = resolve_source(it["path"])
        if src_path is None:
            problems.append(f"{it['id']}: source image not found ({it['path']})")
            continue
        log(f"{it['id']}: hashing {src_path.name}")
        sha = _sha_file(src_path)
        want = (it.get("hash") or {}).get("sha256")
        entry = {"id": it["id"], "name": it.get("name"), "kind": it.get("kind"), "source_name": src_path.name,
                 "recorded_path": it["path"], "bytes": src_path.stat().st_size, "sha256": sha,
                 "matches_seizure_hash": (sha == want) if want else None}
        if it.get("kind") == "video-evidence" and it.get("filesystem"):
            entry["method"] = "filesystem cluster runs (The Sleuth Kit)"
            from .fsrecover import Volume
            vol = Volume(str(src_path))
            files = [f for f in vol.walk() if not f["dir"] and f["size"] and f.get("inode")]
            pending = {k["original_sha256"]: k for k in c["clips"] if k.get("evidence") == it["id"] and k.get("original_sha256")}
            with open(src_path, "rb") as fh:
                for f in files:
                    if not pending:
                        break
                    try:
                        runs = vol.runs(f["inode"])
                    except OSError:
                        continue
                    rng = [[vol.offset + a * vol.block_size, n * vol.block_size] for a, n in runs]
                    if not rng:
                        continue
                    h, n = _hash_ranges(fh, rng, size_limit=f["size"])
                    if h in pending:
                        k = pending.pop(h)
                        cap_clips.append({"id": k["id"], "evidence": it["id"], "kind": "file", "path": f["path"],
                                          "deleted": f["deleted"], "ranges": rng, "size": f["size"], "bytes": n,
                                          "expect_sha256": h, "mp4_sha256": k.get("mp4_sha256"), "built_ok": True})
            for k in pending.values():
                problems.append(f"{k['id']}: no file on the image rebuilds its recorded hash")
        elif it.get("kind") == "video-evidence":
            entry["method"] = "whole file"
            for k in (x for x in c["clips"] if x.get("evidence") == it["id"]):
                cap_clips.append({"id": k["id"], "evidence": it["id"], "kind": "file", "ranges": [[0, entry["bytes"]]],
                                  "size": entry["bytes"], "bytes": entry["bytes"], "expect_sha256": sha,
                                  "mp4_sha256": k.get("mp4_sha256"), "built_ok": sha == k.get("original_sha256", sha)})
        else:
            rec_mode = (it.get("recovery") or {}).get("mode")
            mode = "universal" if rec_mode == "universal" else "auto"
            log(f"{it['id']}: re-running the {mode} parser")
            with Source(str(src_path)) as src:
                det, rec = recover(src, mode=mode, attribute=(mode == "universal"))
                entry["method"] = f"{rec.plugin} ({rec.metadata.get('mode')})"
                entry["family"] = det["family"]
                for cl in build_clips(rec.frames):
                    cid = f"{it['id']}-{cl.id}" if multi else cl.id
                    k = by_id.get(cid)
                    if not k or not k.get("raw_sha256"):
                        continue
                    rng = _merge([(o, n) for f in cl.frames for o, n in f.es])
                    h = hashlib.sha256()
                    for o, n in rng:
                        h.update(bytes(src.buf[o:o + n]))
                    ok = h.hexdigest() == k["raw_sha256"]
                    if not ok:
                        problems.append(f"{cid}: re-parse does not rebuild the exported stream")
                    cap_clips.append({"id": cid, "evidence": it["id"], "kind": f"raw {k.get('codec', 'h264')}", "ranges": rng,
                                      "frames": len(cl.frames), "bytes": sum(n for _, n in rng),
                                      "expect_sha256": k["raw_sha256"], "mp4_sha256": k.get("mp4_sha256"),
                                      "built_ok": ok})
        cap_items.append(entry)
    secret, public = ed25519.examiner_key()
    body = {"format": FORMAT, "definition": DEFINITION, "case_id": c["case"]["case_id"],
            "examiner": c["case"].get("examiner"), "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool": f"Sarvadristhi {__version__}", "items": cap_items, "clips": cap_clips, "problems": problems,
            "public_key": public.hex(), "key_fingerprint": ed25519.fingerprint(public)}
    msg = _canon(body)
    out = case_dir / "capsule"
    out.mkdir(exist_ok=True)
    (out / "capsule.json").write_text(json.dumps(body, indent=1), encoding="utf-8")
    sig = ed25519.sign(secret, msg)
    (out / "capsule.sig").write_text(sig.hex(), encoding="ascii")
    summary = {"clips": len(cap_clips), "built_ok": sum(k["built_ok"] for k in cap_clips),
               "ranges": sum(len(k["ranges"]) for k in cap_clips), "items": len(cap_items), "problems": problems,
               "capsule_sha256": hashlib.sha256(msg).hexdigest(), "key_fingerprint": body["key_fingerprint"],
               "seconds": round(time.time() - t0, 1)}
    Ledger(case_dir / "custody.jsonl").append("replay-capsule-built", **{k: v for k, v in summary.items() if k != "problems"},
                                              problems=len(problems))
    return summary


def load(cap_dir: Path) -> tuple[dict, bool]:
    cap_dir = Path(cap_dir)
    body = json.loads((cap_dir / "capsule.json").read_text(encoding="utf-8"))
    sig = bytes.fromhex((cap_dir / "capsule.sig").read_text(encoding="ascii").strip())
    return body, ed25519.verify(bytes.fromhex(body["public_key"]), _canon(body), sig)


def replay(cap_dir: Path, images: dict | None = None, tamper: str | None = None, log=print) -> dict:
    """Rebuild every clip from the image using only the capsule. `tamper` names one clip whose first
    byte range gets one byte flipped in memory: a demonstration that replay catches a change."""
    cap_dir = Path(cap_dir)
    t0 = time.time()
    body, sig_ok = load(cap_dir)
    images = images or {}
    items, results = {}, []
    for it in body["items"]:
        p = Path(images[it["id"]]) if it["id"] in images else resolve_source(it["recorded_path"], near=cap_dir)
        st = {"id": it["id"], "name": it.get("name"), "found": bool(p), "path": str(p) if p else None}
        if p:
            log(f"{it['id']}: hashing {p.name}")
            st["sha256_ok"] = _sha_file(p) == it["sha256"]
        items[it["id"]] = (p, st)
    for k in body["clips"]:
        p, st = items.get(k["evidence"], (None, {}))
        r = {"id": k["id"], "evidence": k["evidence"], "kind": k["kind"], "ranges": len(k["ranges"]), "bytes": k["bytes"]}
        if not p:
            r.update(ok=False, why="image not found")
        else:
            with open(p, "rb") as f:
                h, n = _hash_ranges(f, k["ranges"], size_limit=k.get("size"), flip=100 if tamper == k["id"] else None)
            r.update(ok=h == k["expect_sha256"], sha256=h, expect=k["expect_sha256"], tampered=tamper == k["id"])
            if not r["ok"]:
                r["why"] = "simulated: one byte flipped" if r["tampered"] else "hash differs"
        results.append(r)
    return {"signature_ok": sig_ok, "key_fingerprint": body["key_fingerprint"], "case_id": body["case_id"],
            "created_utc": body["created_utc"], "definition": body["definition"],
            "items": [s for _, s in items.values()], "clips": results,
            "reproduced": sum(r["ok"] for r in results), "total": len(results), "seconds": round(time.time() - t0, 1)}
