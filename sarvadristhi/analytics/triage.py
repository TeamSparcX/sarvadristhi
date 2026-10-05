"""AI triage over recovered clips: people, faces, objects and optional captions.

Backends, best available first (all local, nothing leaves the machine):
  objects/people : RF-DETR (Apache-2.0, `pip install rfdetr`)  -> fallback OpenCV HOG people
  faces          : YuNet (Apache-2.0, ONNX path via DVRF_YUNET) -> fallback OpenCV Haar cascade
  captions       : Qwen3-VL through a local Ollama server, only for motion-event frames
Detections are leads with a model name and confidence, never findings. Accuracy of these
models on CCTV footage has NOT been validated by this project."""
import os
import subprocess

import cv2
import numpy as np

from . import adapters

MAX_FRAMES = 120


class _Backends:
    def __init__(self):
        self.objects = adapters.Detector()
        self.hog = None
        if not self.objects.ok:
            self.hog = cv2.HOGDescriptor()
            self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        yunet = os.environ.get("DVRF_YUNET")
        self.yunet = cv2.FaceDetectorYN.create(yunet, "", (320, 320)) if yunet and os.path.exists(yunet) else None
        self.haar = None if self.yunet else cv2.CascadeClassifier(
            os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))

    def describe(self) -> dict:
        return {"objects": "rf-detr" if self.objects.ok else "opencv-hog-people",
                "faces": "yunet" if self.yunet else "opencv-haar-frontalface"}

    def run(self, bgr) -> list[dict]:
        out = []
        if self.objects.ok:
            for d in self.objects.detect(bgr):
                out.append({"what": f"coco:{d['class_id']}", "box": d["box"], "confidence": d["confidence"],
                            "model": "rf-detr"})
        else:
            rects, weights = self.hog.detectMultiScale(bgr, winStride=(8, 8), padding=(8, 8), scale=1.05)
            for (x, y, w, h), s in zip(rects, np.ravel(weights) if len(rects) else []):
                if s > 0.5:
                    out.append({"what": "person", "box": [int(x), int(y), int(x + w), int(y + h)],
                                "confidence": round(float(min(0.99, s / 2)), 2), "model": "opencv-hog"})
        if self.yunet is not None:
            h, w = bgr.shape[:2]
            self.yunet.setInputSize((w, h))
            _, faces = self.yunet.detect(bgr)
            for f in faces if faces is not None else []:
                out.append({"what": "face", "box": [float(v) for v in f[:4]], "confidence": float(f[-1]),
                            "model": "yunet"})
        else:
            g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            for (x, y, w, h) in self.haar.detectMultiScale(g, 1.1, 6, minSize=(24, 24)):
                out.append({"what": "face", "box": [int(x), int(y), int(x + w), int(y + h)],
                            "confidence": None, "model": "opencv-haar"})
        return out


def _sample(mp4: str, times: list[float]):
    for t in times:
        r = subprocess.run(["ffmpeg", "-v", "quiet", "-ss", f"{t:.2f}", "-i", mp4, "-frames:v", "1",
                            "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
        if r.stdout:
            yield t, cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)


def triage_clip(mp4: str, duration_s: float, events: list[dict], backends: _Backends | None = None,
                every_s: float = 5.0, captions: bool = False) -> dict:
    b = backends or _Backends()
    times = sorted({round(t, 2) for e in events for t in (e["start_s"], (e["start_s"] + e["end_s"]) / 2, e["end_s"])}
                   | {round(k * every_s, 2) for k in range(int(duration_s // every_s) + 1)})[:MAX_FRAMES]
    hits, caps = [], []
    for t, img in _sample(mp4, times):
        for d in b.run(img):
            hits.append({"t_s": t, **d})
        if captions and any(e["start_s"] <= t <= e["end_s"] for e in events):
            c = adapters.describe(img)
            if c.get("ok"):
                caps.append({"t_s": t, "model": c["model"], "text": c["text"]})
    return {"backends": b.describe(), "frames_examined": len(times), "detections": hits, "captions": caps}
