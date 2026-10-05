"""Render per-camera H.264 with burned-in camera label, camera clock, and a moving subject."""
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Windows: prefer the OCR-tuned DejaVu font when installed for this user.
FONT_CANDIDATES = [
    str(Path.home() / "AppData/Local/Microsoft/Windows/Fonts/DejaVuSansMono.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "C:/Windows/Fonts/DejaVuSansMono.ttf",
    "C:/Windows/Fonts/consola.ttf",
]


@dataclass
class Camera:
    channel: int          # 1-based, as a recorder shows it
    label: str            # burned-in camera name
    source: str           # ffmpeg lavfi background
    drift_s: int          # camera clock minus true time, seconds
    subject_at: float     # seconds after start when the subject enters frame
    subject_secs: float = 4.0
    post: str = ""        # optional filter on the background (e.g. sensor noise)
    overlay: bool = True                  # burn in label and clock
    prnu_seed: int | None = None          # simulated sensor fingerprint (multiplicative, luma)
    prnu_strength: float = 0.03
    change_at_frac: float | None = None   # camera clock changed at this fraction of the recording
    changed_drift_s: int = 0              # drift after the change

    def drift_at(self, t: float, seconds: float) -> int:
        if self.change_at_frac is not None and t >= self.change_at_s(seconds):
            return self.changed_drift_s
        return self.drift_s

    def change_at_s(self, seconds: float) -> int:
        return int(seconds * self.change_at_frac)


def font_path() -> str:
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return p
    raise SystemExit("No monospace TTF font found; edit FONT_CANDIDATES in evidence_builder/footage.py")


def require_ffmpeg():
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not found on PATH")


def _clock(font: str, epoch: int, enable: str = "") -> str:
    en = f":enable='{enable}'" if enable else ""
    return (f"drawtext=fontfile='{font}':text='%{{pts\\:gmtime\\:{epoch}}}'"
            f":x=w-tw-12:y=h-th-10:fontsize=20:fontcolor=white:box=1:boxcolor=black@0.85:boxborderw=4{en}")


def prnu_pattern(seed: int, width: int, height: int, out: Path) -> Path:
    """Deterministic per-camera fixed-pattern noise image (grey 128 = no change)."""
    import numpy as np
    import cv2
    k = np.clip(128 + np.random.default_rng(seed).normal(0, 40, (height, width)), 0, 255).astype(np.uint8)
    cv2.imwrite(str(out), k)
    return out


def encoder(codec: str, fps: int) -> list[str]:
    """Deterministic single-thread encoders; one keyframe per second, no B-frames, AUDs and
    parameter sets repeated on every keyframe (as recorders write them)."""
    if codec == "hevc":
        return ["-c:v", "libx265", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                "-x265-params", f"repeat-headers=1:aud=1:bframes=0:keyint={fps}:min-keyint={fps}:scenecut=0"
                                ":pools=1:frame-threads=1:wpp=0:log-level=error",
                "-b:v", "350k", "-f", "hevc"]
    return ["-c:v", "libx264", "-threads", "1", "-preset", "veryfast", "-profile:v", "main", "-pix_fmt", "yuv420p",
            "-bf", "0", "-g", str(fps), "-keyint_min", str(fps), "-sc_threshold", "0",
            "-x264-params", "repeat-headers=1", "-b:v", "350k", "-maxrate", "450k", "-bufsize", "450k",
            "-bsf:v", "h264_metadata=aud=insert", "-f", "h264"]


def render(cam: Camera, true_start_epoch: int, seconds: int, fps: int,
           width: int, height: int, out: Path, codec: str = "h264") -> None:
    if codec != "h264" and not cam.overlay:
        raise ValueError("H.265 test footage is rendered with overlays only")
    if not cam.overlay:
        return _render_plain(cam, seconds, fps, width, height, out)
    # Windows: FFmpeg filter arguments treat a drive-letter colon as a separator.
    font = font_path().replace("\\", "/").replace(":", r"\:")
    t0, t1 = cam.subject_at, cam.subject_at + cam.subject_secs
    speed = (width + 60) / cam.subject_secs
    clock_epoch = true_start_epoch + cam.drift_s
    bg = f"[0:v]{cam.post}[bg];[bg]" if cam.post else "[0:v]"
    graph = (
        f"{bg}[1:v]overlay=x='-60+(t-{t0})*{speed:.3f}':y={height // 2 - 20}"
        f":enable='between(t,{t0},{t1})'[s];"
        f"[s]drawtext=fontfile='{font}':text='{cam.label}':x=12:y=10:fontsize=20"
        f":fontcolor=white:box=1:boxcolor=black@0.85:boxborderw=4,"
        + (_clock(font, clock_epoch) if cam.change_at_frac is None else
           _clock(font, clock_epoch, f"lt(t,{cam.change_at_s(seconds)})") + "," +
           _clock(font, true_start_epoch + cam.changed_drift_s, f"gte(t,{cam.change_at_s(seconds)})"))
        + "[v]"
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"{cam.source}{':' if '=' in cam.source else '='}size={width}x{height}:rate={fps}",
        "-f", "lavfi", "-i", f"color=c=red:size=60x120:rate={fps}",
        "-filter_complex", graph, "-map", "[v]", "-t", str(seconds),
        *encoder(codec, fps), str(out),   # several x264 threads + VBV give different bytes on every run
    ]
    subprocess.run(cmd, check=True)


ENCODE = ["-c:v", "libx264", "-threads", "1", "-preset", "veryfast", "-profile:v", "main", "-pix_fmt", "yuv420p", "-bf", "0"]


def _render_plain(cam: Camera, seconds: int, fps: int, width: int, height: int, out: Path) -> None:
    """No overlay; optional sensor fingerprint. Used for the PRNU attribution scenario."""
    inputs = ["-f", "lavfi", "-i", f"{cam.source}{':' if '=' in cam.source else '='}size={width}x{height}:rate={fps}"]
    bg = f"[0:v]{cam.post + ',' if cam.post else ''}format=yuv420p[bg]"
    if cam.prnu_seed is not None:
        pat = prnu_pattern(cam.prnu_seed, width, height, out.with_suffix(".prnu.png"))
        inputs += ["-loop", "1", "-i", str(pat)]
        graph = (f"{bg};[1:v]format=gray,format=yuv420p[k];[bg][k]blend=c0_expr='A*(1+(B-128)/128*"
                 f"{cam.prnu_strength})':c1_expr='A':c2_expr='A'[v]")
    else:
        graph = bg.replace("[bg]", "[v]")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", graph,
           "-map", "[v]", "-t", str(seconds), *ENCODE, "-g", str(fps), "-keyint_min", str(fps),
           "-sc_threshold", "0", "-x264-params", "repeat-headers=1", "-b:v", "350k", "-maxrate", "450k",
           "-bufsize", "450k", "-bsf:v", "h264_metadata=aud=insert", "-f", "h264", str(out)]
    subprocess.run(cmd, check=True)
