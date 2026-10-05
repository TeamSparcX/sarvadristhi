"""Expert Witness Format (E01) acquisition and reading via libewf (ewf-tools, pyewf).

acquire_e01: image a source to E01 with embedded MD5/SHA-256 and case metadata, then
             verify with ewfverify and record both in the custody ledger.
expand:      stream an E01 to a raw working copy, recomputing hashes and comparing them
             with the hashes stored inside the E01 (any mismatch is fatal)."""
import hashlib
import re
import shutil
import subprocess
from pathlib import Path

from .custody import Ledger

CHUNK = 8 * 1024 * 1024


def available() -> bool:
    return bool(shutil.which("ewfacquire") and shutil.which("ewfverify"))


def acquire_e01(source: str, target_base: Path, ledger: Ledger, case_id: str = "", examiner: str = "",
                evidence: str = "", description: str = "DVR/NVR storage") -> dict:
    if not available():
        raise RuntimeError("ewf-tools not installed (ewfacquire, ewfverify)")
    target_base = Path(target_base)
    if target_base.with_suffix(".E01").exists():
        raise FileExistsError(f"{target_base}.E01 exists; refusing to overwrite evidence")
    ledger.append("e01-acquisition-start", source=source, target=str(target_base) + ".E01")
    cmd = ["ewfacquire", "-u", "-t", str(target_base), "-f", "encase6", "-c", "fast", "-d", "sha256",
           "-C", case_id or "-", "-D", description, "-e", examiner or "-", "-E", evidence or "-",
           "-N", "Sarvadristhi acquisition", "-S", "2GiB", source]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        ledger.append("e01-acquisition-failed", stderr=r.stderr[-500:])
        raise RuntimeError(r.stderr[-500:] or "ewfacquire failed")
    e01 = target_base.with_suffix(".E01")
    v = verify(e01)
    out = {"source": source, "image": str(e01), "format": "EWF (EnCase 6)", **v}
    ledger.append("e01-acquisition-complete", **out)
    return out


def verify(e01: Path) -> dict:
    r = subprocess.run(["ewfverify", "-q", "-d", "sha256", str(e01)], capture_output=True, text=True)
    text = r.stdout + r.stderr
    get = lambda pat: (m.group(1).lower() if (m := re.search(pat, text, re.I)) else None)
    return {"ewfverify_ok": r.returncode == 0 and "SUCCESS" in text.upper(),
            "md5": get(r"MD5 hash (?:stored|calculated)[^:]*:\s*([0-9a-f]{32})"),
            "sha256": get(r"SHA256 hash (?:stored|calculated)[^:]*:\s*([0-9a-f]{64})")}


def stored_hashes(e01: Path) -> dict:
    import pyewf
    h = pyewf.handle()
    h.open(pyewf.glob(str(e01)))
    try:
        out = {}
        for k in ("MD5", "SHA1", "SHA256"):
            try:
                v = h.get_hash_value(k)
                if v:
                    out[k.lower()] = v.lower()
            except Exception:
                pass
        return out
    finally:
        h.close()


def expand(e01: Path, raw: Path, ledger: Ledger | None = None) -> dict:
    """Write the media stream of an E01 to a raw file; verify against stored hashes."""
    import pyewf
    raw = Path(raw)
    raw.parent.mkdir(parents=True, exist_ok=True)
    h = pyewf.handle()
    h.open(pyewf.glob(str(e01)))
    md5, sha = hashlib.md5(), hashlib.sha256()
    try:
        size = h.get_media_size()
        with raw.open("xb") as f:
            pos = 0
            while pos < size:
                b = h.read(min(CHUNK, size - pos))
                if not b:
                    break
                md5.update(b)
                sha.update(b)
                f.write(b)
                pos += len(b)
    finally:
        h.close()
    stored = stored_hashes(e01)
    computed = {"md5": md5.hexdigest(), "sha256": sha.hexdigest()}
    match = all(computed[k] == stored[k] for k in computed if k in stored) and bool(stored)
    out = {"e01": str(e01), "raw": str(raw), "bytes": pos, "stored": stored, "computed": computed,
           "hashes_match": match}
    if ledger:
        ledger.append("e01-expanded", **out)
    if not match:
        raise ValueError(f"E01 content does not match its stored hashes: {stored} vs {computed}")
    return out
