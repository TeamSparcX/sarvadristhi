"""Poisoned-evidence scenario: REAL surveillance footage, deliberately tampered recorders.

Footage: six real cameras from the MEVA dataset (Kitware / IARPA, CC-BY-4.0), 2018-03-07
16:50-16:53, with human activity labels for scoring. The recorder disks and every act of
tampering are simulated in real vendor formats, and each one is listed in scenario.json so
the tool's findings can be checked against what was planted:

  DVR-A  Hikvision layout, 4 channels   formatted after the incident; recording resumed and
                                        overwrote the earliest footage; CH3 clock set back
                                        15 min mid-recording; CH2/CH4 clocks drift
  DVR-B  Dahua/CP Plus layout, 2 ch.    part of the disk overwritten; a frame torn at the boundary
  USB    NTFS stick (built by make_scenario_usb.ps1, elevated)
         - export of CH1 (the vehicle entry) DELETED after the incident: the only surviving copy
           of footage the DVR format destroyed
         - export of CH3 EDITED: the 4 s showing the door entry were cut out
         - export of CH4 DISGUISED as maintenance_log.txt

    python -m pramanshala.scenario kit/meva kit/scenario"""
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from . import hikfs
from .annexb import split_access_units
from .build import MIB, _summary, _write_gt, build_dahua, build_hik
from .footage import Camera, _clock, font_path, require_ffmpeg

UTC = timezone.utc
SECONDS, FPS, W, H = 180, 10, 1280, 720
DATE = "2018-03-07"

# channel, label, MEVA source camera, file start (true), drift, clock change (frac, drift after)
DVR_A = [(1, "CAM-01 PARKING", "school.G328", "16-50-01", 0, None),
         (2, "CAM-02 ENTRANCE ROAD", "school.G336", "16-50-06", 47, None),
         (3, "CAM-03 ADMIN DOOR", "admin.G326", "16-50-01", 0, (0.7, -900)),
         (4, "CAM-04 STAIRWELL", "admin.G329", "16-50-00", -312, None)]
DVR_B = [(1, "CAM-01 BUS LOBBY", "bus.G331", "16-50-00", 0, None),
         (2, "CAM-02 HOSPITAL REAR", "hospital.G301", "16-50-07", 12, None)]


def epoch(hms: str) -> int:
    return int(datetime.fromisoformat(f"{DATE}T{hms.replace('-', ':')}").replace(tzinfo=UTC).timestamp())


def meva_file(src: Path, cam: str) -> Path:
    hits = sorted(src.glob(f"{DATE}.*.{cam}.r13.avi"))
    if not hits:
        raise SystemExit(f"missing MEVA clip for {cam} in {src} (run python -m pramanshala.fetch_meva)")
    return hits[0]


class RealClip:
    """A camera's access units encoded from real footage, with a DVR-style overlay."""

    def __init__(self, cam: Camera, src: Path, start_epoch: int, offset_s: float, seconds: int, work: Path, tag: str,
                 codec: str = "h264"):
        es = work / f"{tag}.{'h265' if codec == 'hevc' else 'h264'}"
        enc = (["-c:v", "libx264", "-threads", "1", "-preset", "veryfast",   # deterministic output
                "-bf", "0", "-g", str(FPS), "-keyint_min", str(FPS), "-sc_threshold", "0",
                "-x264-params", "repeat-headers=1", "-b:v", "900k", "-maxrate", "1200k", "-bufsize", "1200k",
                "-bsf:v", "h264_metadata=aud=insert", "-f", "h264"] if codec != "hevc" else
               ["-c:v", "libx265", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-x265-params", f"repeat-headers=1:aud=1:bframes=0:keyint={FPS}:min-keyint={FPS}:scenecut=0"
                                ":pools=1:frame-threads=1:wpp=0:log-level=error", "-b:v", "700k", "-f", "hevc"])
        if not es.exists():
            font = font_path().replace("\\", "/").replace(":", r"\:")
            clock = (_clock(font, start_epoch + cam.drift_s) if cam.change_at_frac is None else
                     _clock(font, start_epoch + cam.drift_s, f"lt(t,{cam.change_at_s(seconds)})") + "," +
                     _clock(font, start_epoch + cam.changed_drift_s, f"gte(t,{cam.change_at_s(seconds)})"))
            graph = (f"[0:v]fps={FPS},scale={W}:{H},setpts=PTS-STARTPTS,"
                     f"drawtext=fontfile='{font}':text='{cam.label}':x=16:y=14:fontsize=28:fontcolor=white"
                     f":box=1:boxcolor=black@0.85:boxborderw=5,{clock.replace('fontsize=20', 'fontsize=28')}[v]")
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(offset_s), "-t", str(seconds),
                            "-i", str(src), "-filter_complex", graph, "-map", "[v]", "-an", *enc, str(es)], check=True)
        self.cam, self.fps, self.w, self.h, self.codec = cam, FPS, W, H, codec
        raw = es.read_bytes()
        self.es_sha256 = hashlib.sha256(raw).hexdigest()
        self.aus = split_access_units(raw, codec)
        self.t_ms = [start_epoch * 1000 + i * 1000 // FPS for i in range(len(self.aus))]
        self.start_epoch, self.seconds = start_epoch, seconds
        self.source = src.name

    # the DVR writers only need these
    def overlay_ms(self, i):
        return self.t_ms[i] + 1000 * self.cam.drift_at((self.t_ms[i] - self.start_epoch * 1000) / 1000, self.seconds)

    def gops(self):
        cur = []
        for i, au in enumerate(self.aus):
            if au.keyframe and cur:
                yield cur
                cur = []
            cur.append(i)
        if cur:
            yield cur

    def dhav_frame(self, i):
        from .build import Clip
        return Clip.dhav_frame(self, i)

    def ps_frame(self, i, base_ms):
        from .build import Clip
        return Clip.ps_frame(self, i, base_ms)

    def segment(self, t0: float, t1: float, cut: tuple[float, float] | None = None) -> bytes:
        """Annex-B bytes of whole GOPs between t0 and t1 s (optionally with [cut) GOPs removed)."""
        out = []
        for g in self.gops():
            t = g[0] / FPS
            if t0 <= t < t1 and not (cut and cut[0] <= t < cut[1]):
                out += [self.aus[i].data for i in g]
        return b"".join(out)


def _mp4(es: bytes, dst: Path, work: Path):
    tmp = work / (dst.stem + ".h264")
    tmp.write_bytes(es)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-r", str(FPS), "-f", "h264", "-i", str(tmp),
                    "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(dst)], check=True)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def build(src_dir: Path, out: Path) -> dict:
    require_ffmpeg()
    work = out / "_work"
    work.mkdir(parents=True, exist_ok=True)
    (out / "images").mkdir(exist_ok=True)
    (out / "ground_truth").mkdir(exist_ok=True)
    usb = out / "usb_files"
    usb.mkdir(exist_ok=True)

    def clips_for(spec, prefix):
        made = []
        for ch, label, cam, hms, drift, change in spec:
            c = Camera(ch, label, "", drift, 0.0, change_at_frac=change[0] if change else None,
                       changed_drift_s=change[1] if change else 0)
            print(f"  encoding {prefix} CH{ch} {label} <- MEVA {cam}", flush=True)
            made.append(RealClip(c, meva_file(src_dir, cam), epoch(hms), 0, SECONDS, work, f"{prefix}_ch{ch:02d}"))
        return made

    print("DVR-A (Hikvision layout) ...", flush=True)
    a = clips_for(DVR_A, "A")
    end_s = max(c.start_epoch for c in a) + SECONDS
    format_time = end_s + 300                       # 16:58: the recorder is formatted
    resumed_start = format_time + 60
    # recording resumes on CH1 and CH2: the same real cameras, later in their own footage
    res_a = [RealClip(Camera(1, "CAM-01 PARKING", "", 0, 0.0), meva_file(src_dir, "school.G328"),
                      resumed_start, SECONDS, 115, work, "A_resumed_ch01"),
             RealClip(Camera(2, "CAM-02 ENTRANCE ROAD", "", 47, 0.0), meva_file(src_dir, "school.G336"),
                      resumed_start, SECONDS, 115, work, "A_resumed_ch02")]
    layout = hikfs.Layout(block_size=4 * MIB, pages=1)
    rows_a = []
    img_a = out / "images" / "DVR-A_hikvision.img"
    info_a = build_hik(a, res_a, layout, img_a, rows_a, "overwritten", min(c.start_epoch for c in a) - 60,
                       end_s, format_time)
    _write_gt(out / "ground_truth" / "DVR-A_hikvision.csv", rows_a, hik=True)

    print("DVR-B (Dahua / CP Plus layout) ...", flush=True)
    b = clips_for(DVR_B, "B")
    res_b = RealClip(Camera(1, "CAM-01 BUS LOBBY", "", 0, 0.0), meva_file(src_dir, "bus.G331"),
                     max(c.start_epoch for c in b) + SECONDS + 420, SECONDS, 60, work, "B_resumed_ch01")
    rows_b = []
    img_b = out / "images" / "DVR-B_dahua.img"
    info_b = build_dahua(b, res_b, img_b, rows_b, "overwritten")
    _write_gt(out / "ground_truth" / "DVR-B_dahua.csv", rows_b, hik=False)

    # survival of CH1 per second on DVR-A decides which part of the export is the only copy
    ch1 = [r for r in rows_a if r[1] == 1 and r[9] == "original"]
    destroyed_t = sorted((r[3] - a[0].start_epoch * 1000) / 1000 for r in ch1 if r[8] == "destroyed")

    print("USB stick files ...", flush=True)
    for p in usb.iterdir():
        p.unlink()
    exp1 = usb / "EXPORT_CH01_PARKING_20180307_165030.mp4"     # vehicle entry 16:50:46, reversing 16:51:05
    _mp4(a[0].segment(29, 90), exp1, work)
    exp3 = usb / "EXPORT_CH03_ADMINDOOR_20180307_165100.mp4"   # door entry at 16:51:26-30 cut out
    _mp4(a[2].segment(59, 120, cut=(84, 90)), exp3, work)
    disguised = usb / "maintenance_log.txt"                   # really an MP4 of the stairwell
    _mp4(a[3].segment(78, 108), disguised, work)
    (usb / "README.txt").write_text("Shift B exports for the security office.\r\n", encoding="ascii")

    poisons = [
        {"id": "P1", "evidence": "DVR-A", "what": "Recorder formatted after the incident",
         "planted": datetime.fromtimestamp(format_time, UTC).isoformat(), "expect": "format-event + index-reset findings"},
        {"id": "P2", "evidence": "DVR-A", "what": "Recording resumed after the format and overwrote the earliest footage",
         "planted": f"CH1 seconds destroyed: {destroyed_t[0]:.0f}-{destroyed_t[-1]:.0f}" if destroyed_t else "none",
         "expect": "carved pre-format footage; frames lost to overwrite"},
        {"id": "P3", "evidence": "DVR-A", "what": "CH3 clock set back 15 minutes mid-recording",
         "planted": "at 126 s, drift 0 -> -900 s", "expect": "clock-change finding on camera 3"},
        {"id": "P4", "evidence": "DVR-A", "what": "Camera clocks drift", "planted": "CH2 +47 s, CH4 -312 s",
         "expect": "clock-drift findings"},
        {"id": "P5", "evidence": "DVR-B", "what": "Part of the Dahua disk overwritten; a frame torn at the boundary",
         "planted": f"{info_b['overwritten_ranges']}", "expect": "torn-frame finding; gap"},
        {"id": "P6", "evidence": "USB", "what": "Export of the vehicle entry deleted after the incident",
         "planted": exp1.name, "expect": "deleted file recovered intact with deletion time; matched to DVR-A CH1"},
        {"id": "P7", "evidence": "USB", "what": "Export of the admin door edited: 6 s showing the entry removed",
         "planted": f"{exp3.name}: 16:51:25-16:51:31 cut", "expect": "provenance gap vs DVR-A CH3"},
        {"id": "P8", "evidence": "USB", "what": "Video disguised as a text file", "planted": disguised.name,
         "expect": "signature/extension mismatch; analysed as video"},
    ]
    meta = {
        "scenario": "poisoned-evidence bench v1",
        "nature": "REAL footage (MEVA, CC-BY-4.0); recorder disks and all tampering SIMULATED in vendor formats",
        "footage": {"dataset": "MEVA, Kitware / IARPA DIVA", "license": "CC-BY-4.0", "home": "https://mevadata.org",
                    "window": f"{DATE} 16:50:00-16:53:07 (recorder clock)"},
        "video": {"codec": "H.264 Main, 1 s GOP", "fps": FPS, "width": W, "height": H, "seconds": SECONDS},
        "evidence": {
            "DVR-A": {"image": img_a.name, "family": "hikvision", "sha256": _sha(img_a), **info_a, **_summary(rows_a),
                      "channels": [{"channel": c.cam.channel, "label": c.cam.label, "source": c.source,
                                    "true_start_utc": datetime.fromtimestamp(c.start_epoch, UTC).isoformat(),
                                    "drift_s": c.cam.drift_s, "change": [c.cam.change_at_frac, c.cam.changed_drift_s]}
                                   for c in a]},
            "DVR-B": {"image": img_b.name, "family": "dahua-dhav", "sha256": _sha(img_b), **info_b, **_summary(rows_b),
                      "channels": [{"channel": c.cam.channel, "label": c.cam.label, "source": c.source,
                                    "true_start_utc": datetime.fromtimestamp(c.start_epoch, UTC).isoformat(),
                                    "drift_s": c.cam.drift_s} for c in b]},
            "USB": {"image": "usb.vhd (built by pramanshala/make_scenario_usb.ps1)",
                    "files": {p.name: _sha(p) for p in sorted(usb.iterdir())}, "delete": [exp1.name]}},
        "poisons": poisons,
        "labels": "MEVA activity labels: kit/meva/*.activities.yml (frame numbers at 30 fps from each file start)",
    }
    (out / "scenario.json").write_text(json.dumps(meta, indent=2, default=str))
    return meta


if __name__ == "__main__":
    m = build(Path(sys.argv[1]), Path(sys.argv[2]))
    for p in m["poisons"]:
        print(f"{p['id']} {p['evidence']:6} {p['what']} [{p['planted']}]")
    for k, e in m["evidence"].items():
        if "frames_total" in e:
            print(k, {x: e[x] for x in ("frames_total", "frames_intact", "frames_destroyed")})
