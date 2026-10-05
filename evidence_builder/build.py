"""Build the Phase 0 evidence set: per-camera exports plus Dahua- and Hikvision-layout disk
images in intact / formatted / overwritten variants, with frame-level ground truth."""
import csv
import hashlib
import json
import platform
import subprocess
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__, dhav, hikfs, mpegps
from .annexb import split_access_units
from .footage import Camera, render, require_ffmpeg

UTC = timezone.utc
MIB = 1024 * 1024

DEFAULT_CAMERAS = [
    Camera(1, "CAM-01 MAIN GATE", "smptehdbars", 0, 30.0, post="noise=alls=6:allf=t:all_seed=26150",
           change_at_frac=2 / 3, changed_drift_s=-900),   # clock set back 15 min mid-recording
    Camera(2, "CAM-02 PARKING", "life=seed=26150:mold=10:ratio=0.15:life_color=#5a5a5a:death_color=#1e1e1e", 47, 75.0),
    Camera(3, "CAM-03 CORRIDOR", "cellauto=rule=110:random_fill_ratio=0.05:random_seed=26150:scroll=1", -312, 130.0),
]
# Overlay-less cameras filming an IDENTICAL scene; only the sensor fingerprint differs.
NO_OVERLAY = [Camera(k, "", "smptehdbars", 0, 9999.0, post=f"noise=alls=6:allf=t:all_seed={500 + k}",
                     overlay=False, prnu_seed=1000 + k) for k in (1, 2, 3)]
NO_OVERLAY_SECONDS = 60
# Recording that resumes on channel 1 after the recorder is formatted.
RESUMED = Camera(1, "CAM-01 MAIN GATE", "rgbtestsrc", 0, 20.0, post="noise=alls=6:allf=t:all_seed=26151")


def _hash(path: Path) -> tuple[str, str]:
    md5, sha = hashlib.md5(), hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * MIB), b""):
            md5.update(chunk)
            sha.update(chunk)
    return md5.hexdigest(), sha.hexdigest()


def _naive(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(epoch_ms / 1000, UTC).replace(tzinfo=None)


class Clip:
    """One camera's access units with true capture times."""

    def __init__(self, cam: Camera, start_epoch: int, seconds: int, fps: int, w: int, h: int,
                 work: Path, tag: str, codec: str = "h264"):
        es = work / f"{tag}.{'h265' if codec == 'hevc' else 'h264'}"
        render(cam, start_epoch, seconds, fps, w, h, es, codec=codec)
        self.cam, self.fps, self.w, self.h, self.codec = cam, fps, w, h, codec
        self.es_sha256 = hashlib.sha256(es.read_bytes()).hexdigest()
        self.aus = split_access_units(es.read_bytes(), codec)
        self.t_ms = [start_epoch * 1000 + i * 1000 // fps for i in range(len(self.aus))]
        self.start_epoch, self.seconds = start_epoch, seconds

    def overlay_ms(self, i: int) -> int:
        """Time the camera's burned-in clock showed for frame i."""
        return self.t_ms[i] + 1000 * self.cam.drift_at((self.t_ms[i] - self.start_epoch * 1000) / 1000, self.seconds)

    def gops(self):
        """Yield lists of frame indices, each starting at a keyframe."""
        cur = []
        for i, au in enumerate(self.aus):
            if au.keyframe and cur:
                yield cur
                cur = []
            cur.append(i)
        if cur:
            yield cur

    def dhav_frame(self, i: int) -> bytes:
        au = self.aus[i]
        return dhav.build_frame(self.cam.channel - 1, i, _naive(self.t_ms[i]),
                                self.t_ms[i] % 65536, au.data, au.keyframe, self.w, self.h, self.fps)

    def ps_frame(self, i: int, base_ms: int) -> bytes:
        return mpegps.wrap_access_unit(self.aus[i].data, (self.t_ms[i] - base_ms) * 90 + 90000,
                                       self.aus[i].keyframe, 0x24 if self.codec == "hevc" else 0x1B)


def _interleaved_gops(clips: list[Clip]):
    """(clip, gop) in true-time order, as a recorder writing all channels at once would."""
    items = [(c.t_ms[g[0]], k, c, g) for k, c in enumerate(clips) for g in c.gops()]
    for _, _, c, g in sorted(items, key=lambda x: (x[0], x[1])):
        yield c, g


def _status(off: int, length: int, damage: list[tuple[int, int]]) -> str:
    for a, b in damage:
        if off < b and a < off + length:
            return "destroyed"
    return "intact"


# --------------------------------------------------------------------------- Dahua-layout image
def build_dahua(clips, resumed, out: Path, gt_rows: list, variant: str) -> dict:
    header = MIB
    data = bytearray()
    placed = []  # (clip, frame_idx, abs_offset, length)
    for c, g in _interleaved_gops(clips):
        for i in g:
            fr = c.dhav_frame(i)
            placed.append((c, i, header + len(data), len(fr)))
            data += fr
    size = header + ((len(data) + 8 * MIB) // MIB + 1) * MIB
    damage, resumed_placed = [], []
    if variant == "overwritten":
        a = header + int(len(data) * 0.40)
        b = a + int(len(data) * 0.25)
        damage.append((a, b))
        new = bytearray()
        for i in range(len(resumed.aus)):
            fr = resumed.dhav_frame(i)
            if len(new) + len(fr) > b - a:
                new += fr[:b - a - len(new)]          # torn frame at the overwrite boundary
                break
            resumed_placed.append((resumed, i, a + len(new), len(fr)))
            new += fr
        data[a - header:b - header] = new.ljust(b - a, b"\x00")
    with out.open("wb") as f:
        f.truncate(size)
        f.seek(header)
        f.write(data)
    for c, i, off, ln in placed:
        gt_rows.append([out.name, c.cam.channel, i, c.t_ms[i], c.overlay_ms(i), int(c.aus[i].keyframe), off, ln,
                        _status(off, ln, damage), "original"])
    for c, i, off, ln in resumed_placed:
        gt_rows.append([out.name, c.cam.channel, i, c.t_ms[i], c.overlay_ms(i), int(c.aus[i].keyframe), off, ln,
                        "intact", "post-format recording"])
    return {"data_region": [header, header + len(data)], "overwritten_ranges": damage,
            "filesystem_metadata": "none emulated (DHFS layout unverified); DHAV frames only"}


# --------------------------------------------------------------------------- Hikvision-layout image
def _fill_blocks(clips, layout, blocks, free, owner_tag, placed):
    base_ms = min(c.t_ms[0] for c in clips)
    current = {}
    for c, g in _interleaved_gops(clips):
        payload = b"".join(c.ps_frame(i, base_ms) for i in g)
        ch = c.cam.channel
        idx = current.get(ch)
        if idx is None or blocks[idx].room(layout, 1) < len(payload):
            if not free:
                raise SystemExit("Image full: raise --pages")
            idx = free.pop(0)
            blocks[idx] = hikfs.Block(channel=ch)
            current[ch] = idx
        blk = blocks[idx]
        if blk.start == hikfs.NO_TIME:
            blk.start = c.t_ms[g[0]] // 1000
        blk.end = c.t_ms[g[-1]] // 1000
        blk.idrs.append((c.t_ms[g[0]] // 1000, len(blk.data)))
        pos = len(blk.data)
        for i in g:
            fr = c.ps_frame(i, base_ms)
            placed.append((c, i, idx, pos, len(fr), owner_tag))
            pos += len(fr)
        blk.data += payload


def build_hik(clips, resumed, layout, out: Path, gt_rows: list, variant: str,
              power_on: int, end_s: int, format_time: int) -> dict:
    old = [hikfs.Block() for _ in range(layout.total_blocks)]
    old_placed = []
    _fill_blocks(clips, layout, old, list(range(layout.total_blocks)), "original", old_placed)
    logs = [(power_on, hikfs.LOG_POWER, "Power On")]
    for c in clips:
        ch = c.cam.channel
        logs += [(c.t_ms[0] // 1000, hikfs.LOG_HDD, f"Start Recording CH{ch:02d}"),
                 (c.t_ms[0] // 1000 + int(c.cam.subject_at), hikfs.LOG_MOTION, f"Start Motion Detect CH{ch:02d}"),
                 (end_s, hikfs.LOG_HDD, f"Stop Recording CH{ch:02d}")]

    new = [hikfs.Block() for _ in range(layout.total_blocks)]
    new_placed = []
    init_time, indexed = power_on, set()
    if variant == "intact":
        indexed = {i for i, b in enumerate(old) if b.channel}
    else:                                   # a format resets the index; video blocks are left as-is
        init_time = format_time
        logs.append((format_time, hikfs.LOG_HDD, "HDD Initialize"))
    if variant == "overwritten":            # recording resumes from block 0 after the format
        resumed_all = resumed if isinstance(resumed, list) else [resumed]   # one or more channels resume
        _fill_blocks(resumed_all, layout, new, list(range(layout.total_blocks)), "post-format recording", new_placed)
        for r in resumed_all:
            logs.append((r.t_ms[0] // 1000, hikfs.LOG_HDD, f"Start Recording CH{r.cam.channel:02d}"))
        indexed = {i for i, b in enumerate(new) if b.channel}

    damage = {}                             # block idx -> [(start, end) relative ranges overwritten]
    for i, b in enumerate(new):
        if b.channel:
            tab = len(hikfs.ofni_table(b))
            damage[i] = [(0, len(b.data)), (layout.block_size - tab, layout.block_size)]

    index_view = [new[i] if new[i].channel else old[i] if i in indexed else hikfs.Block()
                  for i in range(layout.total_blocks)]
    with out.open("wb") as f:
        f.truncate(layout.capacity)
        f.seek(hikfs.MS_OFFSET)
        f.write(hikfs.master_sector(layout, init_time))
        f.seek(layout.logs_offset)
        f.write(hikfs.system_logs(logs)[:layout.logs_size])
        for base in (layout.btree1_offset, layout.btree2_offset):
            f.seek(base)
            f.write(hikfs.hikbtree(layout, base, index_view, init_time))
        for blocks in (old, new):           # old data first, then whatever overwrote it
            for i, b in enumerate(blocks):
                if not b.channel:
                    continue
                off = layout.block_offset(i)
                f.seek(off)
                f.write(b.data)
                tab = hikfs.ofni_table(b)
                f.seek(off + layout.block_size - len(tab))
                f.write(tab)

    for c, i, idx, pos, ln, tag in old_placed:
        hit = any(pos < b and a < pos + ln for a, b in damage.get(idx, []))
        status = "destroyed" if hit else "intact"
        gt_rows.append([out.name, c.cam.channel, i, c.t_ms[i], c.overlay_ms(i), int(c.aus[i].keyframe),
                        layout.block_offset(idx) + pos, ln, status, "original", idx,
                        int(status == "intact" and idx in indexed and not new[idx].channel)])
    for c, i, idx, pos, ln, tag in new_placed:
        gt_rows.append([out.name, c.cam.channel, i, c.t_ms[i], c.overlay_ms(i), int(c.aus[i].keyframe),
                        layout.block_offset(idx) + pos, ln, "intact", tag, idx, 1])
    return {"layout": asdict(layout), "total_blocks": layout.total_blocks,
            "blocks_with_original_video": sorted({p[2] for p in old_placed}),
            "blocks_overwritten": sorted(damage),
            "index_state": {"intact": "complete", "formatted": "reset by format",
                            "overwritten": "describes post-format recording only"}[variant],
            "init_time_utc": datetime.fromtimestamp(init_time, UTC).isoformat(), "logs": len(logs)}


# --------------------------------------------------------------------------- orchestration
def build(out_dir: Path, seconds: int = 180, fps: int = 10, width: int = 640, height: int = 360,
          start: str = "2026-09-28T21:14:00", pages: int = 1, block_mib: int = 4,
          cameras: list[Camera] | None = None) -> dict:
    require_ffmpeg()
    cameras = cameras or DEFAULT_CAMERAS
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "_work"
    work.mkdir(exist_ok=True)
    start_dt = datetime.fromisoformat(start).replace(tzinfo=UTC)
    start_s = int(start_dt.timestamp())
    end_s = start_s + seconds
    format_time = end_s + 600
    resumed_start = format_time + 90
    resumed_secs = max(60, seconds * 2 // 3)

    print(f"Rendering {len(cameras)} cameras x {seconds}s ...")
    clips = [Clip(c, start_s, seconds, fps, width, height, work, f"ch{c.channel:02d}") for c in cameras]
    resumed = Clip(RESUMED, resumed_start, resumed_secs, fps, width, height, work, "resumed_ch01")

    exports = {"dahua": out_dir / "exports" / "dahua", "hikvision": out_dir / "exports" / "hikvision"}
    for p in exports.values():
        p.mkdir(parents=True, exist_ok=True)
    files = {}
    for c in clips:
        slug = c.cam.label.replace(" ", "_")
        dav = exports["dahua"] / f"ch{c.cam.channel:02d}_{slug}.dav"
        dav.write_bytes(b"".join(c.dhav_frame(i) for i in range(len(c.aus))))
        ps = exports["hikvision"] / f"ch{c.cam.channel:02d}_{slug}.ps"
        ps.write_bytes(b"".join(c.ps_frame(i, c.t_ms[0]) for i in range(len(c.aus))))
        files[str(dav.relative_to(out_dir))] = {"frames": len(c.aus)}
        files[str(ps.relative_to(out_dir))] = {"frames": len(c.aus)}

    print("Rendering overlay-less cameras (sensor-fingerprint scenario) ...")
    from dataclasses import replace
    plain = [Clip(c, start_s, NO_OVERLAY_SECONDS, fps, width, height, work, f"plain_ch{c.channel:02d}")
             for c in NO_OVERLAY]
    control = [Clip(replace(c, prnu_seed=None), start_s, NO_OVERLAY_SECONDS, fps, width, height, work,
                    f"control_ch{c.channel:02d}") for c in NO_OVERLAY]
    img_dir = out_dir / "images"
    img_dir.mkdir(exist_ok=True)
    gt_dir = out_dir / "ground_truth"
    gt_dir.mkdir(exist_ok=True)
    layout = hikfs.Layout(block_size=block_mib * MIB, pages=pages)
    images = {}
    for variant in ("intact", "overwritten"):
        rows = []
        p = img_dir / f"dahua_{variant}.img"
        print(f"Writing {p.name}")
        info = build_dahua(clips, resumed, p, rows, variant)
        _write_gt(gt_dir / f"{p.stem}.csv", rows, hik=False)
        images[p.name] = {"family": "dahua-dhav", "variant": variant, **info, **_summary(rows)}
    for name, cl, note in (("dahua_no_overlay", plain, "no overlay; per-camera sensor fingerprint; identical scene"),
                           ("dahua_no_overlay_control", control, "no overlay; NO sensor fingerprint (control)")):
        rows = []
        p = img_dir / f"{name}.img"
        print(f"Writing {p.name}")
        info = build_dahua(cl, None, p, rows, "intact")
        _write_gt(gt_dir / f"{p.stem}.csv", rows, hik=False)
        images[p.name] = {"family": "dahua-dhav", "variant": "no-overlay", "note": note, **info, **_summary(rows)}
    for variant in ("intact", "formatted", "overwritten"):
        rows = []
        p = img_dir / f"hikvision_{variant}.img"
        print(f"Writing {p.name}")
        info = build_hik(clips, resumed, layout, p, rows, variant, start_s - 60, end_s, format_time)
        _write_gt(gt_dir / f"{p.stem}.csv", rows, hik=True)
        images[p.name] = {"family": "hikvision", "variant": variant, **info, **_summary(rows)}

    print("Hashing ...")
    hashes, gt_files = [], {}
    # only this build's outputs: the folder may also hold other data (scenario, footage)
    own = [p for d in ("images", "exports", "ground_truth") for p in (out_dir / d).rglob("*")]
    for p in sorted(own):
        if p.is_file() and "_work" not in p.parts and p.suffix in (".img", ".dav", ".ps", ".csv") \
                and (p.suffix != ".img" or p.name in images) and (p.suffix != ".csv" or p.parent.name == "ground_truth"):
            md5, sha = _hash(p)
            rel = str(p.relative_to(out_dir))
            hashes.append(f"{sha}  {rel}")
            entry = (images.get(p.name) if p.suffix == ".img" else
                     gt_files.setdefault(rel, {}) if p.suffix == ".csv" else files[rel])
            entry.update({"bytes": p.stat().st_size, "md5": md5, "sha256": sha})
    (out_dir / "SHA256SUMS").write_text("\n".join(hashes) + "\n")

    manifest = {
        "kit": "evidence_builder", "kit_version": __version__,
        "built_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "host": platform.platform(), "ffmpeg": _ffmpeg_version(),
        "nature": "SYNTHETIC CONTENT in vendor-exact containers. Not real evidence.",
        "time_basis": "All times UTC. DHAV dates and HIKBTREE times are recorder (true) time; "
                      "burned-in overlay clock = true time + camera drift_s.",
        "video": {"codec": "H.264 Main, no B-frames, 1 s GOP, SPS/PPS before every IDR, AUD per frame",
                  "fps": fps, "width": width, "height": height, "seconds": seconds},
        "cameras": [{**asdict(c.cam), "frames": len(c.aus), "true_start_utc": start_dt.isoformat(),
                     "elementary_stream_sha256": c.es_sha256} for c in clips],
        "events": [{"what": "red subject crosses frame left to right", "channel": c.cam.channel,
                    "true_start_utc": (start_dt + timedelta(seconds=c.cam.subject_at)).isoformat(),
                    "overlay_clock_shows": (start_dt + timedelta(seconds=c.cam.subject_at + c.cam.drift_at(c.cam.subject_at, seconds))).isoformat(),
                    "duration_s": c.cam.subject_secs} for c in clips],
        "clock_changes": [{"channel": c.cam.channel, "at_true_utc": (start_dt + timedelta(seconds=c.cam.change_at_s(seconds))).isoformat(),
                           "drift_before_s": c.cam.drift_s, "drift_after_s": c.cam.changed_drift_s}
                          for c in clips if c.cam.change_at_frac is not None],
        "anti_forensic_story": {
            "recording_ends_utc": datetime.fromtimestamp(end_s, UTC).isoformat(),
            "recorder_formatted_utc": datetime.fromtimestamp(format_time, UTC).isoformat(),
            "recording_resumes_utc": datetime.fromtimestamp(resumed_start, UTC).isoformat(),
            "resumed_channel": 1, "resumed_seconds": resumed_secs},
        "exports": files, "images": images, "ground_truth": gt_files,
        "fidelity": {
            "verified_against_independent_tools": [
                "DHAV frames: decoded by FFmpeg's dhav demuxer",
                "MPEG-PS: decoded by FFmpeg's mpegps demuxer",
                "Hikvision layout: parsed by akira7799/hikvision-dvr-parser (run tests/verify.py)"],
            "emulated_not_verified": [
                "DHAV header checksum byte (FFmpeg ignores it)",
                "Hikvision fields the reference parser marks unknown are zero",
                "System log description strings are evidence_builder-authored",
                "Behaviour of a real format (index reset, video kept, logs kept) is an assumption"],
            "not_emulated": ["Dahua DHFS filesystem metadata", "Hikvision 'IMKH' stream header",
                             "Hikvision SQLite recording database", "Audio"]},
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


def _write_gt(path: Path, rows, hik: bool):
    cols = ["image", "channel", "frame_no", "true_time_ms_utc", "overlay_time_ms_utc", "keyframe", "offset", "length",
            "status", "origin"] + (["block", "index_confirmed"] if hik else [])
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)


def _summary(rows) -> dict:
    intact = [r for r in rows if r[8] == "intact"]
    return {"frames_total": len(rows), "frames_intact": len(intact),
            "frames_destroyed": len(rows) - len(intact),
            "original_frames_recoverable": sum(1 for r in intact if r[9] == "original")}


def _ffmpeg_version() -> str:
    try:
        return subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.split("\n")[0]
    except OSError:
        return "unknown"
