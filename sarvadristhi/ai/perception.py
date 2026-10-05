"""Layer 2 - perception: what is in each frame, and which detections are the same object.

  objects  RF-DETR (Apache-2.0, COCO classes) on the GPU when available; OpenCV HOG people
           detector otherwise. The backend actually used is recorded with every detection.
  tracks   greedy IoU association per class across sampled frames (a detection joins the
           track it overlaps most if IoU >= 0.3 and the gap is <= 2 s).
  faces    YuNet detector + SFace 128-d embedding (Apache-2.0, OpenCV zoo) on person tracks.
  looks    HSV colour histograms of the upper and lower half of each person/vehicle crop:
           a transparent appearance signature used for cross-camera matching in layer 4.
Everything here is a measurement with a confidence, never a conclusion."""
import os
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np

KEEP = {"person", "bicycle", "car", "motorcycle", "bus", "truck", "backpack", "handbag", "suitcase",
        "umbrella", "knife", "cell phone", "dog", "laptop"}
VEHICLE_CLASSES = {"car", "truck", "bus", "motorcycle"}
MODELS = Path(__file__).resolve().parents[2] / "models"


def _coco() -> dict:
    try:
        from rfdetr.assets.coco_classes import COCO_CLASSES
        return COCO_CLASSES
    except ImportError:
        return {}


class Detector:
    def __init__(self, threshold: float = 0.45):
        self.threshold, self.names, self.error = threshold, _coco(), None
        self.model, self.backend, self.device = None, "opencv-hog-people", "cpu"
        try:
            import torch
            from rfdetr import RFDETRBase
            self.model = RFDETRBase()
            self.device = "cuda:" + torch.cuda.get_device_name(0) if torch.cuda.is_available() else "cpu"
            self.backend = "rf-detr-base (COCO, 80 classes)"
        except Exception as e:      # package or weights missing: say so, fall back
            self.error = str(e)[:200]
            self.hog = cv2.HOGDescriptor()
            self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    def __call__(self, bgr) -> list[dict]:
        if self.model is not None:
            from PIL import Image
            d = self.model.predict(Image.fromarray(bgr[:, :, ::-1]), threshold=self.threshold)
            out = []
            for b, c, s in zip(d.xyxy, d.class_id, d.confidence):
                name = self.names.get(int(c), f"class-{int(c)}")
                if name in KEEP:
                    out.append({"cls": name, "box": [round(float(v), 1) for v in b], "conf": round(float(s), 3)})
            return out
        rects, w = self.hog.detectMultiScale(bgr, winStride=(8, 8), padding=(8, 8), scale=1.05)
        return [{"cls": "person", "box": [float(x), float(y), float(x + ww), float(y + hh)],
                 "conf": round(float(min(0.99, s / 2)), 3)}
                for (x, y, ww, hh), s in zip(rects, np.ravel(w) if len(rects) else []) if s > 0.5]


class Faces:
    def __init__(self):
        det = MODELS / "face_detection_yunet_2023mar.onnx"
        rec = MODELS / "face_recognition_sface_2021dec.onnx"
        self.ok = det.exists() and rec.exists()
        if self.ok:
            self.det = cv2.FaceDetectorYN.create(str(det), "", (320, 320), 0.7)
            self.rec = cv2.FaceRecognizerSF.create(str(rec), "")

    def __call__(self, bgr) -> list[dict]:
        if not self.ok:
            return []
        h, w = bgr.shape[:2]
        self.det.setInputSize((w, h))
        _, faces = self.det.detect(bgr)
        out = []
        for f in faces if faces is not None else []:
            emb = self.rec.feature(self.rec.alignCrop(bgr, f)).flatten()
            out.append({"box": [float(f[0]), float(f[1]), float(f[0] + f[2]), float(f[1] + f[3])],
                        "conf": round(float(f[-1]), 3), "embedding": emb})
        return out


def iou(a, b) -> float:
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def appearance(bgr, box) -> dict | None:
    """Upper/lower-half HSV histograms + dominant colour names of a crop."""
    x0, y0, x1, y1 = [int(v) for v in box]
    crop = bgr[max(0, y0):max(0, y1), max(0, x0):max(0, x1)]
    if crop.size == 0 or crop.shape[0] < 12 or crop.shape[1] < 6:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h = hsv.shape[0]
    parts = {"upper": hsv[int(h * 0.15):int(h * 0.5)], "lower": hsv[int(h * 0.5):int(h * 0.9)]}
    out = {}
    for k, p in parts.items():
        if p.size == 0:
            return None
        hist = cv2.calcHist([p], [0, 1, 2], None, [12, 4, 4], [0, 180, 0, 256, 0, 256])
        out[k] = cv2.normalize(hist, None).flatten()
        out[k + "_colour"] = colour_name(p)
    return out


def colour_name(hsv_patch) -> str:
    h, s, v = [float(np.median(hsv_patch[..., i])) for i in range(3)]
    if v < 60:
        return "black"
    if s < 40:
        return "white" if v > 180 else "grey"
    for lim, name in ((8, "red"), (22, "orange"), (34, "yellow"), (85, "green"), (130, "blue"), (160, "purple"), (180, "red")):
        if h < lim:
            return name
    return "red"


def frames(mp4: str, fps: float):
    """Yield (t_s, bgr) at a fixed sampling rate through one FFmpeg process."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "csv=p=0", mp4], capture_output=True, text=True)
    w, h = map(int, r.stdout.strip().split(",")[:2])
    p = subprocess.Popen(["ffmpeg", "-v", "quiet", "-i", mp4, "-vf", f"fps={fps}", "-f", "rawvideo",
                          "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE)
    n, k = w * h * 3, 0
    while True:
        b = p.stdout.read(n)
        if len(b) < n:
            break
        yield round(k / fps, 2), np.frombuffer(b, np.uint8).reshape(h, w, 3).copy()
        k += 1
    p.wait()


def perceive(clip: dict, mp4: str, det: Detector, faces: Faces, thumbs: Path, fps: float = 2.0,
             mask: list | None = None) -> dict:
    """Run detection, tracking, faces and appearance over one clip."""
    t_start = time.time()
    tracks, dets_out, n_frames = [], [], 0
    mask = mask or []
    for t, img in frames(mp4, fps):
        n_frames += 1
        found = [d for d in det(img) if not any(iou(d["box"], m) > 0.3 for m in mask)]
        claimed = set()
        for d in sorted(found, key=lambda d: -d["conf"]):
            best, bi = None, 0.0
            for tr in tracks:
                gap_max = 6.0 if d["cls"] in VEHICLE_CLASSES else 2.0   # parked vehicles flicker in and out
                if tr["cls"] != d["cls"] or not 0 < t - tr["last_t"] <= gap_max or id(tr) in claimed:
                    continue
                pb = tr["boxes"][-1][1]
                v = iou(pb, d["box"])
                # centre distance in box heights; a walker covers ~0.8 heights per second
                hgt = max(1.0, pb[3] - pb[1], d["box"][3] - d["box"][1])
                dist = np.hypot((pb[0] + pb[2] - d["box"][0] - d["box"][2]) / 2,
                                (pb[1] + pb[3] - d["box"][1] - d["box"][3]) / 2) / hgt
                allowed = 0.35 + 0.8 * (t - tr["last_t"])
                size = min(hgt, pb[3] - pb[1] + 1) / max(hgt, 1)
                sc = v + max(0.0, 1 - dist / allowed) * size
                if (v >= 0.2 or dist <= allowed) and sc > bi:
                    best, bi = tr, sc
            if best is None:
                best = {"id": f"{clip['id']}-T{len(tracks) + 1}", "cls": d["cls"], "first_t": t, "last_t": t,
                        "boxes": [], "confs": [], "best": None, "looks": [], "faces": []}
                tracks.append(best)
            claimed.add(id(best))
            best["boxes"].append((t, d["box"]))
            best["confs"].append(d["conf"])
            best["last_t"] = t
            area = (d["box"][2] - d["box"][0]) * (d["box"][3] - d["box"][1])
            score = d["conf"] * area
            if best["best"] is None or score > best["best"][0]:
                best["best"] = (score, t, d["box"], img)
            if d["cls"] in ("person", "car", "truck", "bus", "motorcycle", "bicycle") and len(best["looks"]) < 12:
                a = appearance(img, d["box"])
                if a:
                    best["looks"].append(a)
            dets_out.append({"t": t, "cls": d["cls"], "box": d["box"], "conf": d["conf"], "track": best["id"]})
    out_tracks = []
    for tr in tracks:
        if len(tr["boxes"]) < 3:          # blips under 3 sightings are noise until confirmed
            continue
        _, bt, bbox, bimg = tr["best"]
        thumb = thumbs / f"{tr['id']}.jpg"
        crop_full = bimg.copy()
        x0, y0, x1, y1 = [int(v) for v in bbox]
        cv2.rectangle(crop_full, (x0, y0), (x1, y1), (0, 0, 255), 2)
        cv2.imwrite(str(thumb), crop_full)
        pad = 12
        crop = bimg[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad]
        if crop.size:
            cv2.imwrite(str(thumbs / f"{tr['id']}_crop.jpg"), crop)
        fs = faces(bimg) if tr["cls"] == "person" else []
        fs = [f for f in fs if bbox[0] - 5 <= (f["box"][0] + f["box"][2]) / 2 <= bbox[2] + 5
              and bbox[1] - 5 <= (f["box"][1] + f["box"][3]) / 2 <= bbox[1] + (bbox[3] - bbox[1]) * 0.45]
        looks = tr["looks"]
        sig = None
        if looks:
            sig = {k: np.mean([l[k] for l in looks], axis=0) for k in ("upper", "lower")}
            for k in ("upper_colour", "lower_colour"):
                vals = [l[k] for l in looks]
                sig[k] = max(set(vals), key=vals.count)
        (ex0, ey0, ex1, ey1) = tr["boxes"][0][1]
        (lx0, ly0, lx1, ly1) = tr["boxes"][-1][1]
        out_tracks.append({
            "id": tr["id"], "clip": clip["id"], "channel": clip.get("channel"), "cls": tr["cls"],
            "first_t": tr["first_t"], "last_t": tr["last_t"], "duration_s": round(tr["last_t"] - tr["first_t"], 2),
            "observations": len(tr["boxes"]), "mean_conf": round(float(np.mean(tr["confs"])), 3),
            "max_conf": round(float(np.max(tr["confs"])), 3), "best_t": bt, "best_box": bbox,
            "entry_box": [ex0, ey0, ex1, ey1], "exit_box": [lx0, ly0, lx1, ly1],
            "frame_size": list(bimg.shape[1::-1]),
            "thumb": f"ai/thumbs/{thumb.name}", "crop": f"ai/thumbs/{tr['id']}_crop.jpg",
            "upper_colour": sig and sig["upper_colour"], "lower_colour": sig and sig["lower_colour"],
            "_sig": sig, "_face": fs[0]["embedding"] if fs else None,
            "face": {"found": bool(fs), "conf": fs[0]["conf"] if fs else None},
            "path": [[t, [round(v) for v in b]] for t, b in tr["boxes"]],
        })
    return {"clip": clip["id"], "frames_examined": n_frames, "sample_fps": fps,
            "detections": dets_out, "tracks": out_tracks, "seconds": round(time.time() - t_start, 2),
            "noise_tracks_dropped": len(tracks) - len(out_tracks)}
