"""Structural integrity of an exported video when no recorder copy exists to compare against.

A recorder writes one encoder's output: the same parameter sets throughout, a regular keyframe
cadence and timestamps that advance one frame at a time. Joining footage from elsewhere, cutting
a span out, or re-timing tends to break one of these. Each break is reported as a possible edit,
with the frame where it occurs; none is proof by itself (a recorder can legitimately change
resolution or drop frames under load), so results are warnings for an examiner."""
import hashlib
import json
import statistics
import subprocess
from pathlib import Path

from .formats.codec import H264, HEVC, is_vcl, nal_type, sniff


def _nals(es: bytes):
    i, n = 0, len(es)
    starts = []
    while True:
        j = es.find(b"\x00\x00\x01", i)
        if j < 0 or j + 3 >= n:
            break
        starts.append(j + 3)
        i = j + 3
    for k, s in enumerate(starts):
        e = starts[k + 1] - 3 if k + 1 < len(starts) else n
        while e > s and es[e - 1] == 0:
            e -= 1
        yield s, e


def _pts_gaps(video: Path, fps: float | None) -> list[dict]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
                        "-of", "json", str(video)], capture_output=True, text=True)
    try:
        pts = sorted(float(p["pts_time"]) for p in json.loads(r.stdout or "{}").get("packets", []) if p.get("pts_time"))
    except (ValueError, KeyError):
        return []
    if len(pts) < 3:
        return []
    steps = [b - a for a, b in zip(pts, pts[1:])]
    step = statistics.median(steps) or (1 / fps if fps else 0.04)
    return [{"frame": k + 1, "at_s": round(pts[k], 3), "gap_s": round(d, 3)}
            for k, d in enumerate(steps) if d > max(3 * step, step + 0.5)]


def check(video: Path, annexb: bytes | None = None, fps: float | None = None) -> dict:
    from .provenance import annexb_of
    video = Path(video)
    es = annexb if annexb is not None else annexb_of(video)
    if not es:
        return {"ok": None, "reason": "no elementary video could be read"}
    codec = sniff(es)
    sps_type = 33 if codec == HEVC else 7
    frame, sps_seen, sps_changes, keys = -1, [], [], []
    for s, e in _nals(es):
        b0 = es[s]
        t = nal_type(b0, codec)
        if t == sps_type:
            h = hashlib.sha1(es[s:e]).hexdigest()[:12]
            if sps_seen and h != sps_seen[-1]["sha1"]:
                sps_changes.append({"before_frame": frame + 1, "from": sps_seen[-1]["sha1"], "to": h})
            if not sps_seen or h != sps_seen[-1]["sha1"]:
                sps_seen.append({"sha1": h, "first_frame": frame + 1})
        if is_vcl(b0, codec):
            hdr = 2 if codec == HEVC else 1
            if s + hdr < e and es[s + hdr] & 0x80:            # first slice of a new picture
                frame += 1
                if (codec == H264 and t == 5) or (codec == HEVC and 16 <= t <= 23):
                    keys.append(frame)
    gops = [b - a for a, b in zip(keys, keys[1:])]
    irregular = []
    if len(gops) >= 4:
        med = statistics.median(gops)
        irregular = [{"after_frame": keys[k], "gop_frames": g, "typical": med}
                     for k, g in enumerate(gops) if g < 0.5 * med or g > 1.5 * med]
    gaps = _pts_gaps(video, fps) if video.suffix.lower() in (".mp4", ".mov", ".mkv", ".ts") else []
    distinct = len({x["sha1"] for x in sps_seen})
    return {"codec": codec, "frames": frame + 1, "keyframes": len(keys), "sps_variants": distinct,
            "sps_changes": sps_changes, "gop_median": statistics.median(gops) if gops else None,
            "gop_irregular": irregular[:20], "timestamp_gaps": gaps[:20],
            "ok": not (sps_changes or irregular or gaps)}


def findings(name: str, r: dict) -> list[dict]:
    out = []
    if r.get("sps_changes"):
        c = r["sps_changes"][0]
        out.append({"kind": "export-structure", "severity": "warning",
                    "summary": f"{name}: encoder parameter sets change at frame {c['before_frame']} "
                               f"({r['sps_variants']} different sets): footage from different encoder settings is joined, "
                               f"a possible splice", "detail": {"file": name, **{k: r[k] for k in ("codec", "sps_changes")}}})
    if r.get("timestamp_gaps"):
        g = r["timestamp_gaps"][0]
        out.append({"kind": "export-structure", "severity": "warning",
                    "summary": f"{name}: timestamps jump {g['gap_s']} s at {g['at_s']} s (frame {g['frame']}): "
                               f"a possible cut", "detail": {"file": name, "gaps": r["timestamp_gaps"]}})
    if r.get("gop_irregular"):
        g = r["gop_irregular"][0]
        out.append({"kind": "export-structure", "severity": "info",
                    "summary": f"{name}: keyframe spacing breaks the recorder's cadence after frame {g['after_frame']} "
                               f"({g['gop_frames']} frames vs typical {g['typical']:g})",
                    "detail": {"file": name, "irregular": r["gop_irregular"]}})
    return out
