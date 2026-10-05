"""Verify a built pramanshala against INDEPENDENT tools.

  1. FFmpeg's dhav and mpegps demuxers decode every export with the expected frame count.
  2. FFmpeg's dhav demuxer reads the Dahua intact image data region (packet count).
  3. akira7799/hikvision-dvr-parser (MIT, real-DVR-derived) parses each Hikvision image:
     master sector, system logs, HIKBTREE entries and OFNI IDR tables vs ground truth.
  4. The pramanshala's own dual-signature DHAV carver recovers exactly the ground-truth intact set.

Usage: python tests/verify.py OUT_DIR [--parser PATH_TO_hikvision-dvr-parser]
"""
import argparse
import contextlib
import csv
import io
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pramanshala import dhav  # noqa: E402

PARSER_REPO = "https://github.com/akira7799/hikvision-dvr-parser"
results = []


def check(name, ok, detail=""):
    results.append({"check": name, "pass": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def ffprobe_frames(path: Path, fmt: str, extra=()) -> int:
    r = subprocess.run(["ffprobe", "-v", "error", *extra, "-f", fmt, "-count_frames", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return int(r.stdout.strip() or 0)


def ffmpeg_clean(path: Path, fmt: str) -> bool:
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", fmt, "-i", str(path), "-f", "null", "-"],
                       capture_output=True, text=True)
    return r.returncode == 0 and not r.stderr.strip()


def gt(out: Path, stem: str):
    with (out / "ground_truth" / f"{stem}.csv").open() as f:
        return list(csv.DictReader(f))


def verify_exports(out: Path, m: dict):
    for rel, meta in m["exports"].items():
        p = out / rel
        fmt = "dhav" if p.suffix == ".dav" else "mpeg"
        n = ffprobe_frames(p, fmt)
        check(f"export {p.name}: FFmpeg {fmt} decodes {meta['frames']} frames",
              n == meta["frames"] and ffmpeg_clean(p, fmt), f"read {n}")


def verify_dahua(out: Path, m: dict):
    for name, img in m["images"].items():
        if img["family"] != "dahua-dhav":
            continue
        p = out / "images" / name
        a, b = img["data_region"]
        rows = gt(out, p.stem)
        if img["variant"] == "intact":
            r = subprocess.run(["ffprobe", "-v", "error", "-skip_initial_bytes", str(a), "-f", "dhav",
                                "-count_packets", "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0",
                                str(p)], capture_output=True, text=True)
            n = int((r.stdout.strip().splitlines() or ["0"])[0] or 0)
            check(f"{name}: FFmpeg dhav demuxer reads every frame in the data region",
                  n == len(rows), f"{n}/{len(rows)} packets")
        buf = p.read_bytes()
        found = {(f.offset, f.length) for f in dhav.scan(buf, a, b)}
        truth = {(int(r["offset"]), int(r["length"])) for r in rows if r["status"] == "intact"}
        fp, fn = found - truth, truth - found
        check(f"{name}: dual-signature carver recovers ground-truth intact frames exactly",
              not fp and not fn, f"recovered {len(found & truth)}/{len(truth)}, false+ {len(fp)}")


def verify_hik(out: Path, m: dict, parser_dir: Path):
    sys.path.insert(0, str(parser_dir))
    from hikvision_dvr_parser import HIKVISIONParser  # noqa: E402
    for name, img in m["images"].items():
        if img["family"] != "hikvision":
            continue
        p = out / "images" / name
        rows = gt(out, p.stem)
        with contextlib.redirect_stdout(io.StringIO()):
            hp = HIKVISIONParser(str(p))
            hp.__enter__()
            ms = hp.parse_master_sector()
            logs = hp.parse_system_logs(limit=1000)
            entries = [e for e in hp.parse_hikbtree() if e.has_video()]
        lay = img["layout"]
        check(f"{name}: master sector at 0x200 with expected geometry",
              ms.offset == 0x200 and ms.data_block_size == lay["block_size"]
              and ms.video_data_offset == lay["video_offset"] and ms.total_data_blocks == img["total_blocks"],
              f"blocks {ms.total_data_blocks}, block size {ms.data_block_size}")
        check(f"{name}: system logs parsed", len(logs) == img["logs"], f"{len(logs)}/{img['logs']}")
        if img["variant"] != "intact":
            check(f"{name}: logs record the format", any("HDD Initialize" in l.description for l in logs))
        expect = defaultdict(list)
        for r in rows:
            if r["index_confirmed"] == "1":
                expect[int(r["block"])].append(r)
        want = {(int(v[0]["channel"]), min(int(x["true_time_ms_utc"]) for x in v) // 1000,
                 max(int(x["true_time_ms_utc"]) for x in v) // 1000,
                 lay["video_offset"] + k * lay["block_size"]) for k, v in expect.items()}
        got = {(e.channel, e.start_time, e.end_time, e.offset) for e in entries}
        check(f"{name}: HIKBTREE entries match ground truth", got == want,
              f"{len(got)} video entries, expected {len(want)}")
        if img["variant"] == "intact":
            ok = True
            for k, v in expect.items():
                with contextlib.redirect_stdout(io.StringIO()):
                    idr = hp.parse_idr_table(lay["video_offset"] + k * lay["block_size"])
                ok &= len(idr) == sum(x["keyframe"] == "1" for x in v)
            check(f"{name}: OFNI IDR tables list every keyframe", ok)
            k0 = min(expect)
            blk = out / "_work" / "verify_block.ps"
            with p.open("rb") as f:
                f.seek(lay["video_offset"] + k0 * lay["block_size"])
                blk.write_bytes(f.read(lay["block_size"]))
            n = ffprobe_frames(blk, "mpeg")
            check(f"{name}: block {k0} extracted by offset decodes as H.264 in MPEG-PS",
                  n == len(expect[k0]), f"{n}/{len(expect[k0])} frames")
        hp.__exit__(None, None, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--parser", type=Path, default=ROOT / "tests" / "_vendor" / "hikvision-dvr-parser")
    a = ap.parse_args()
    m = json.loads((a.out / "manifest.json").read_text())
    if not (a.parser / "hikvision_dvr_parser.py").exists():
        a.parser.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "1", PARSER_REPO, str(a.parser)], check=True)
    commit = subprocess.run(["git", "-C", str(a.parser), "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    verify_exports(a.out, m)
    verify_dahua(a.out, m)
    verify_hik(a.out, m, a.parser)
    passed = sum(r["pass"] for r in results)
    report = {"passed": passed, "total": len(results), "hikvision_parser_commit": commit, "checks": results}
    (a.out / "verify_report.json").write_text(json.dumps(report, indent=2))
    print(f"\n{passed}/{len(results)} checks passed -> {a.out / 'verify_report.json'}")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
