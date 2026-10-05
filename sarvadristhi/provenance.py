"""Clip provenance: prove where an exported clip came from, frame by frame.

A recorder export (MP4/DAV on a USB stick) normally carries the recorder's own H.264 slices,
re-wrapped but not re-encoded. So each coded picture (VCL NAL unit) in the export can be
matched bit-for-bit against the pictures recovered from a recorder disk. The result says:

  matched      these export frames are bit-identical to recorder frames at these recorder times
  export-only  frames found only in the export; if the recorder holds no footage for that period
               on that channel, the export is the only surviving copy
  removed      the export jumps forward in the recorder's frame sequence: frames were cut out

A re-encoded export cannot match bit-for-bit; that is reported as 'no bit-exact match', never
as proof of absence. Hash: SHA-1 of each VCL NAL payload (start codes and parameter sets
excluded), which survives MP4 <-> Annex B re-wrapping."""
import hashlib
import subprocess
import tempfile
from pathlib import Path

VCL = {1, 5}


def vcl_hashes(annexb: bytes) -> list[str]:
    """SHA-1 of every VCL NAL unit (H.264 or H.265), in stream order."""
    from .formats.codec import is_vcl, sniff
    codec = sniff(annexb)
    out, i, n = [], 0, len(annexb)
    starts = []
    while True:
        j = annexb.find(b"\x00\x00\x01", i)
        if j < 0:
            break
        starts.append(j + 3)
        i = j + 3
    for k, s in enumerate(starts):
        e = starts[k + 1] - 3 if k + 1 < len(starts) else n
        while e > s and annexb[e - 1] == 0:      # trailing zero of a 4-byte start code
            e -= 1
        if s < n and is_vcl(annexb[s], codec):
            out.append(hashlib.sha1(annexb[s:e]).hexdigest())
    return out


def annexb_of(video: Path) -> bytes:
    if video.suffix.lower() in (".h264", ".264", ".h265", ".265", ".hevc"):
        return video.read_bytes()
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
                        "-of", "csv=p=0", str(video)], capture_output=True, text=True)
    fmt = "hevc" if r.stdout.strip().startswith("hevc") else "h264"
    with tempfile.TemporaryDirectory() as d:
        dst = Path(d) / f"x.{fmt}"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-map", "0:v:0", "-c", "copy",
                        "-bsf:v", f"{fmt}_mp4toannexb", "-f", fmt, str(dst)], capture_output=True)
        return dst.read_bytes() if dst.exists() else b""


def _hms(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%H:%M:%S.%f")[:-4] if ms is not None else "?"


def match(case_dir: Path, recorder_clips: list[dict], exports: list[dict]) -> list[dict]:
    """recorder_clips: clips with raw_file, start_ms, fps, channel, label, evidence.
    exports: clips from removable media with mp4_file. Returns one result per export."""
    case_dir = Path(case_dir)
    index = {}                              # vcl hash -> (clip, frame index)
    for c in recorder_clips:
        if not c.get("raw_file"):
            continue
        hs = vcl_hashes((case_dir / "clips" / c["raw_file"]).read_bytes())
        for k, h in enumerate(hs):
            index.setdefault(h, (c, k))
    cover = {}                              # channel -> [(start, end)] recorder coverage
    for c in recorder_clips:
        if c.get("start_ms") is not None:
            cover.setdefault(c["channel"], []).append((c["start_ms"], c["end_ms"]))
    results = []
    for x in exports:
        src = case_dir / "clips" / x["mp4_file"]
        hs = vcl_hashes(annexb_of(src))
        hits = [index.get(h) for h in hs]
        n_hit = sum(1 for h in hits if h)
        r = {"export": x["id"], "file": x.get("source_file"), "frames": len(hs), "frames_matched": n_hit,
             "method": "bit-exact match of H.264 VCL NAL units (SHA-1) against recorder frames", "segments": [],
             "edits": [], "export_only": []}
        if not n_hit:
            r["verdict"] = "no bit-exact match (export not from these recorders, or re-encoded)"
            results.append(r)
            continue
        votes = {}
        for h in hits:
            if h:
                votes[h[0]["id"]] = votes.get(h[0]["id"], 0) + 1
        src_clip = max((h[0] for h in hits if h), key=lambda c: votes[c["id"]])
        fps = src_clip.get("fps") or 10
        r.update(source_clip=src_clip["id"], source_label=src_clip.get("label"), source_evidence=src_clip.get("evidence"),
                 channel=src_clip["channel"])
        frame_ms = lambda c, k: c["start_ms"] + int(k * 1000 / (c.get("fps") or fps))
        # anchor: recorder time of export frame i, extrapolated from the nearest match
        anchors = [(i, frame_ms(h[0], h[1])) for i, h in enumerate(hits) if h and h[0]["id"] == src_clip["id"]]

        def t_of(i):
            a = min(anchors, key=lambda p: abs(p[0] - i))
            return a[1] + int((i - a[0]) * 1000 / fps)
        r["start_ms"], r["end_ms"] = t_of(0), t_of(len(hs) - 1)
        # segments of consecutive frames with the same status
        i = 0
        while i < len(hs):
            kind = "matched" if hits[i] else "export-only"
            j = i
            while j + 1 < len(hs) and bool(hits[j + 1]) == bool(hits[i]) and \
                    (not hits[i] or hits[j + 1][0]["id"] == hits[j][0]["id"] and hits[j + 1][1] == hits[j][1] + 1):
                j += 1
            seg = {"kind": kind, "frames": j - i + 1, "export_frames": [i, j]}
            if kind == "matched":
                c, k0 = hits[i]
                seg.update(recorder_clip=c["id"], recorder_frames=[k0, hits[j][1]],
                           from_ms=frame_ms(c, k0), to_ms=frame_ms(c, hits[j][1]))
            else:
                seg.update(from_ms=t_of(i), to_ms=t_of(j))
                recorded = any(a <= seg["from_ms"] and seg["to_ms"] <= b for a, b in cover.get(src_clip["channel"], []))
                seg["recorder_has_period"] = recorded
                seg["only_surviving_copy"] = not recorded
                r["export_only"].append(seg)
            r["segments"].append(seg)
            # a jump forward in the recorder's frame sequence between two matched runs = cut
            if kind == "matched" and j + 1 < len(hs) and hits[j + 1] and hits[j + 1][0]["id"] == hits[j][0]["id"] \
                    and hits[j + 1][1] > hits[j][1] + 1:
                c = hits[j][0]
                gap = hits[j + 1][1] - hits[j][1] - 1
                e = {"after_export_frame": j, "frames_removed": gap, "seconds_removed": round(gap / fps, 2),
                     "recorder_from_ms": frame_ms(c, hits[j][1] + 1), "recorder_to_ms": frame_ms(c, hits[j + 1][1] - 1),
                     "recorder_clip": c["id"]}
                e["summary"] = (f"{e['seconds_removed']} s ({gap} frames) removed: recorder footage "
                                f"{_hms(e['recorder_from_ms'])}-{_hms(e['recorder_to_ms'])} is missing from the export")
                r["edits"].append(e)
            i = j + 1
        only = sum(s["frames"] for s in r["export_only"] if s["only_surviving_copy"])
        if r["edits"]:
            r["verdict"] = f"EDITED: {sum(e['seconds_removed'] for e in r['edits'])} s removed versus {src_clip.get('label')}"
        elif only:
            r["verdict"] = (f"ONLY SURVIVING COPY of {only / fps:.1f} s of {src_clip.get('label')}; "
                            f"{n_hit} frames bit-identical to the recorder")
        elif n_hit == len(hs):
            r["verdict"] = f"EXACT COPY of {src_clip.get('label')} {_hms(r['start_ms'])}-{_hms(r['end_ms'])}"
        else:
            r["verdict"] = f"PARTIAL MATCH ({n_hit}/{len(hs)} frames) with {src_clip.get('label')}"
        results.append(r)
    return results
