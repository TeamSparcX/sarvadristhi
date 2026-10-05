"""Group frames into per-camera clips and export them as playable, hashed MP4."""
import hashlib
import statistics
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .models import EvidenceLevel, Frame

MAX_GAP_MS = 2000


@dataclass
class Clip:
    id: str
    channel: int | None
    label: str | None
    frames: list[Frame] = field(default_factory=list)
    exported: dict = field(default_factory=dict)

    @property
    def start_ms(self):
        return self.frames[0].time_ms

    @property
    def end_ms(self):
        return self.frames[-1].time_ms

    @property
    def level(self) -> EvidenceLevel:
        return min(f.level for f in self.frames)

    @property
    def levels(self) -> dict:
        out = defaultdict(int)
        for f in self.frames:
            out[f.level.label] += 1
        return dict(out)

    @property
    def fps(self) -> float:
        ts = [f.time_ms for f in self.frames if f.time_ms is not None]
        d = [b - a for a, b in zip(ts, ts[1:]) if b > a]
        return round(1000 / statistics.median(d), 3) if d else 25.0

    def summary(self) -> dict:
        return {"id": self.id, "channel": self.channel, "label": self.label, "frames": len(self.frames),
                "keyframes": sum(f.keyframe for f in self.frames), "start_ms": self.start_ms,
                "end_ms": self.end_ms, "fps": self.fps, "evidence_level": self.level.label,
                "levels": self.levels, "time_source": sorted({f.time_source for f in self.frames}),
                "first_offset": min(f.offset for f in self.frames),
                "bytes": sum(f.length for f in self.frames), **self.exported}


def build_clips(frames: list[Frame]) -> list[Clip]:
    timed = defaultdict(list)
    untimed = []
    for f in frames:
        (timed[f.channel] if f.time_ms is not None else untimed).append(f)
    clips = []
    for ch in sorted(timed, key=lambda c: (c is None, c or 0)):
        seq = sorted(timed[ch], key=lambda f: (f.time_ms, f.offset))
        cur = []
        for f in seq:
            if cur and (f.time_ms - cur[-1].time_ms > MAX_GAP_MS or f.time_ms < cur[-1].time_ms):
                clips.append(cur)
                cur = []
            if cur and not cur[0].keyframe and f.keyframe:   # start clips on a keyframe
                clips.append(cur)
                cur = []
            cur.append(f)
        if cur:
            clips.append(cur)
    run = []
    for f in sorted(untimed, key=lambda f: f.offset):      # untimed: contiguous disk runs
        if run and f.offset - (run[-1].offset + run[-1].length) > 64 * 1024:
            clips.append(run)
            run = []
        run.append(f)
    if run:
        clips.append(run)
    out = []
    for k, fr in enumerate(clips):
        ch = fr[0].channel
        out.append(Clip(f"C{k + 1:04d}" + (f"-CH{ch:02d}" if ch is not None and ch < 1000 else ""),
                        ch, fr[0].label, fr))
    return out


def es_of(buf, clip: Clip) -> bytes:
    return b"".join(bytes(buf[o:o + n]) for f in clip.frames for o, n in f.es)


def export(buf, clip: Clip, out_dir: Path) -> dict:
    """Raw .h264 keeps every recovered frame. The MP4 starts at the first keyframe, since
    frames before it reference a picture that no longer exists."""
    out_dir.mkdir(parents=True, exist_ok=True)
    from .formats.codec import ffmpeg_format, sniff
    es = es_of(buf, clip)
    codec = sniff(es)
    raw = out_dir / f"{clip.id}.{'h265' if codec == 'hevc' else 'h264'}"
    raw.write_bytes(es)
    info = {"raw_file": raw.name, "raw_sha256": hashlib.sha256(es).hexdigest(), "codec": codec}
    first = next((i for i, f in enumerate(clip.frames) if f.keyframe), None)
    if first is None:
        info["mp4_error"] = "no keyframe: frames are recovered but cannot be decoded on their own"
        clip.exported = info
        return info
    src = raw
    if first:
        src = out_dir / f"{clip.id}.playable.{raw.suffix[1:]}"
        src.write_bytes(b"".join(bytes(buf[o:o + n]) for f in clip.frames[first:] for o, n in f.es))
        info["mp4_skipped_leading_frames"] = first
    mp4 = out_dir / f"{clip.id}.mp4"
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-fflags", "+genpts", "-r", str(clip.fps),
                        "-f", ffmpeg_format(codec), "-i", str(src), "-c", "copy",
                        *(["-tag:v", "hvc1"] if codec == "hevc" else []), "-movflags", "+faststart", str(mp4)],
                       capture_output=True, text=True)
    if src != raw:
        src.unlink()
    if r.returncode == 0 and mp4.exists():
        info.update(mp4_file=mp4.name, mp4_sha256=hashlib.sha256(mp4.read_bytes()).hexdigest())
    else:
        info["mp4_error"] = r.stderr.strip()[-300:]
    clip.exported = info
    return info
