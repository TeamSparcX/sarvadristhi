"""Optional AI adapters (triage only). Each loads lazily and reports itself unavailable if
its package or weights are missing. NOT exercised by the test suite: model weights cannot
be fetched in the CI sandbox. Validate on real footage before relying on any of them.

  detector : RF-DETR (Apache-2.0)            pip install rfdetr
  faces    : OpenCV YuNet + SFace (Apache-2.0) model files from opencv_zoo
  vlm      : Qwen3-VL via a local Ollama server (Apache-2.0 weights)
"""
import base64
import json
import urllib.request


class Detector:
    """Objects (person, car, bag ...) with RF-DETR. Returns [] if unavailable."""

    def __init__(self, threshold: float = 0.5):
        self.threshold = threshold
        try:
            from rfdetr import RFDETRBase  # noqa: F401
            self.model = RFDETRBase()
            self.ok = True
        except Exception as e:  # missing package or weights
            self.model, self.ok, self.error = None, False, str(e)

    def detect(self, bgr_image):
        if not self.ok:
            return []
        from PIL import Image
        dets = self.model.predict(Image.fromarray(bgr_image[:, :, ::-1]), threshold=self.threshold)
        return [{"box": [float(v) for v in b], "class_id": int(c), "confidence": float(s)}
                for b, c, s in zip(dets.xyxy, dets.class_id, dets.confidence)]


class Faces:
    """Face detection and matching with OpenCV YuNet + SFace (paths to .onnx models)."""

    def __init__(self, yunet_onnx: str, sface_onnx: str):
        import cv2
        try:
            self.det = cv2.FaceDetectorYN.create(yunet_onnx, "", (320, 320))
            self.rec = cv2.FaceRecognizerSF.create(sface_onnx, "")
            self.ok = True
        except Exception as e:
            self.ok, self.error = False, str(e)

    def embed(self, bgr_image):
        if not self.ok:
            return []
        h, w = bgr_image.shape[:2]
        self.det.setInputSize((w, h))
        _, faces = self.det.detect(bgr_image)
        out = []
        for f in faces if faces is not None else []:
            crop = self.rec.alignCrop(bgr_image, f)
            out.append({"box": [float(v) for v in f[:4]], "score": float(f[-1]),
                        "embedding": self.rec.feature(crop)})
        return out

    def similarity(self, e1, e2) -> float:
        import cv2
        return float(self.rec.match(e1, e2, cv2.FaceRecognizerSF_FR_COSINE))


def describe(bgr_image, prompt: str = "Describe people, vehicles and actions in this CCTV frame. "
             "Say 'unclear' when unsure.", model: str = "qwen3-vl:4b",
             host: str = "http://127.0.0.1:11434") -> dict:
    """Caption one frame with a local VLM through Ollama. Returns {'ok': False} if unreachable."""
    import cv2
    ok, png = cv2.imencode(".png", bgr_image)
    body = json.dumps({"model": model, "prompt": prompt, "stream": False,
                       "images": [base64.b64encode(png.tobytes()).decode()]}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{host}/api/generate", body,
                                                           {"Content-Type": "application/json"}), timeout=120) as r:
            return {"ok": True, "model": model, "text": json.loads(r.read())["response"]}
    except Exception as e:
        return {"ok": False, "error": str(e)}
