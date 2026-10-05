"""Layer 3 - scene understanding with a vision-language model (Qwen3-VL via local Ollama).

The VLM is asked a constrained question about ONE detector track at a time: the frame with
the object boxed in red, plus a crop. It must answer in a fixed JSON schema, say whether it
agrees with the detector's class, and list what it is unsure about. Every prompt, image,
raw answer and timing is kept so an examiner can read exactly what the model was shown and
what it said. The answer is then cross-checked against layer 2 (class and colours); a
disagreement lowers confidence and is surfaced for human review rather than resolved
silently. Nothing leaves the machine."""
import base64
import json
import os
import re
import time
import urllib.request

# local Ollama; in Docker the image points this at the host (http://host.docker.internal:11434)
HOST = os.environ.get("SARVADRISTHI_OLLAMA", "http://127.0.0.1:11434")
MODEL = "qwen3-vl:4b"

SYSTEM = ("You are a forensic CCTV analyst. Describe only what is visible in the image. Never guess identity, "
          "ethnicity, age or intent. If something is not clearly visible, say 'unclear'. Answer in JSON only.")

SCHEMA = {
    "type": "object",
    "properties": {
        "subject_class": {"type": "string"},
        "agrees_with_detector": {"type": "boolean"},
        "upper_clothing_colour": {"type": "string"},
        "lower_clothing_colour": {"type": "string"},
        "carrying": {"type": "string"},
        "action": {"type": "string"},
        "scene": {"type": "string"},
        "notable": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "uncertain_aspects": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["subject_class", "agrees_with_detector", "action", "confidence"],
}


def available(model: str = MODEL) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"{HOST}/api/tags", timeout=3) as r:
            names = [m["name"] for m in json.loads(r.read())["models"]]
        return (model in names, "ready" if model in names else f"model {model} not pulled ({', '.join(names)})")
    except Exception as e:
        return False, f"Ollama not reachable at {HOST}: {e}"


def chat(messages: list, schema: dict | None = None, model: str = MODEL, timeout: int = 180) -> dict:
    body = {"model": model, "messages": messages, "stream": False, "think": False,
            "options": {"temperature": 0, "seed": 7, "num_ctx": 4096}}
    if schema:
        body["format"] = schema
    t = time.time()
    req = urllib.request.Request(f"{HOST}/api/chat", json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read())
    m = j["message"]
    text = m.get("content") or ""
    if schema and not text.strip():
        text = m.get("thinking") or ""     # Ollama puts schema-constrained output here when think=False
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    return {"text": text, "seconds": round(time.time() - t, 2),
            "eval_tokens": j.get("eval_count"), "prompt_tokens": j.get("prompt_eval_count")}


def describe_track(track: dict, camera: str, when: str, frame_jpg: bytes, crop_jpg: bytes | None) -> dict:
    prompt = (f"Image 1 is a frame from CCTV camera '{camera}' at {when}. The RED box marks object {track['id']}. "
              f"An object detector labelled it '{track['cls']}' with confidence {track['max_conf']:.2f}. "
              + ("Image 2 is a close crop of the same object. " if crop_jpg else "")
              + "Describe ONLY the object in the red box: what it is, clothing colours (if a person) or body colour "
                "(if a vehicle), what it is carrying, and what it is doing. State whether you agree with the "
                "detector's label. List anything you are unsure about.")
    images = [base64.b64encode(frame_jpg).decode()] + ([base64.b64encode(crop_jpg).decode()] if crop_jpg else [])
    rec = {"track": track["id"], "model": MODEL, "system": SYSTEM, "prompt": prompt,
           "images": [track["thumb"]] + ([track["crop"]] if crop_jpg else []), "schema": "see sarvadristhi/ai/vlm.py SCHEMA"}
    try:
        r = chat([{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": prompt, "images": images}], SCHEMA)
        rec.update(raw=r["text"], seconds=r["seconds"], tokens=r["eval_tokens"])
        m = re.search(r"\{.*\}", r["text"], re.S)
        rec["answer"] = json.loads(m.group(0) if m else r["text"])
        rec["ok"] = True
    except Exception as e:
        rec.update(ok=False, error=str(e)[:300])
    return rec


def cross_check(track: dict, ans: dict) -> dict:
    """Compare the VLM's answer with layer-2 measurements; return agreement factors."""
    checks = []
    vc = (ans.get("subject_class") or "").lower()
    cls_ok = bool(ans.get("agrees_with_detector")) or track["cls"] in vc or \
        (track["cls"] == "person" and any(w in vc for w in ("man", "woman", "person", "pedestrian", "people")))
    checks.append({"what": "object class", "detector": track["cls"], "vlm": ans.get("subject_class"), "agree": cls_ok})
    for k in ("upper", "lower"):
        mine, theirs = track.get(f"{k}_colour"), (ans.get(f"{k}_clothing_colour") or "").lower()
        if mine and theirs and "unclear" not in theirs:
            checks.append({"what": f"{k} colour", "detector": mine, "vlm": theirs,
                           "agree": mine in theirs or theirs in mine or {mine, theirs} <= {"black", "grey", "dark"}})
    agree = sum(c["agree"] for c in checks) / len(checks) if checks else 0.0
    conf = {"high": 1.0, "medium": 0.7, "low": 0.4}.get(ans.get("confidence"), 0.5)
    return {"checks": checks, "agreement": round(agree, 2), "vlm_self_confidence": ans.get("confidence"),
            "needs_review": not cls_ok or conf < 0.5, "score": round(0.6 * agree + 0.4 * conf, 2)}
