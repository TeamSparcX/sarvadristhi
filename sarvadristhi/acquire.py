"""Forensic acquisition: bit-for-bit raw image with MD5 + SHA-256 of the source read and
of the written image, bad-sector logging, and custody entries.

Software read-only opening does not stop every kernel-level write to an attached disk.
Use a hardware write-blocker for original drives; this tool records what it could check."""
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from .custody import Ledger
from .source import Source

CHUNK = 8 * 1024 * 1024
SECTOR = 512


def _read_at(src: Source, offset: int, size: int) -> bytes:
    """Read at an offset on all platforms (Windows has no os.pread)."""
    # Windows: use the source's sector-aligned reader for physical drives.
    if src.is_device and not hasattr(os, "pread"):
        return src.read(offset, size)
    if hasattr(os, "pread"):
        return os.pread(src.fd, size, offset)
    os.lseek(src.fd, offset, os.SEEK_SET)
    return os.read(src.fd, size)


def hash_file(path: Path) -> dict:
    md5, sha = hashlib.md5(), hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            md5.update(b)
            sha.update(b)
    return {"md5": md5.hexdigest(), "sha256": sha.hexdigest()}


def acquire(src_path: str, dst: Path, ledger: Ledger, progress=None) -> dict:
    dst = Path(dst)
    if dst.exists():
        raise FileExistsError(f"{dst} exists; refusing to overwrite evidence")
    with Source(src_path) as src:
        ro = src.device_readonly()
        ledger.append("acquisition-start", source=src_path, bytes=src.size, block_device=src.is_device,
                      kernel_readonly_flag=ro)
        md5, sha = hashlib.md5(), hashlib.sha256()
        bad = []
        started = datetime.now(timezone.utc)
        with open(dst, "xb") as out:
            pos = 0
            while pos < src.size:
                n = min(CHUNK, src.size - pos)
                try:
                    b = _read_at(src, pos, n)
                    if len(b) < n:
                        raise OSError("short read")
                except OSError:
                    b = bytearray()
                    for s in range(pos, pos + n, SECTOR):   # retry sector by sector
                        try:
                            b += _read_at(src, s, SECTOR)
                        except OSError:
                            bad.append(s)
                            b += b"\x00" * SECTOR
                    b = bytes(b[:n])
                md5.update(b)
                sha.update(b)
                out.write(b)
                pos += n
                if progress:
                    progress(pos, src.size)
    src_hash = {"md5": md5.hexdigest(), "sha256": sha.hexdigest()}
    img_hash = hash_file(dst)
    result = {"source": src_path, "image": str(dst), "bytes": os.path.getsize(dst),
              "started_utc": started.isoformat(timespec="seconds"),
              "finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "source_hash": src_hash, "image_hash": img_hash,
              "verified": src_hash == img_hash, "unreadable_sectors": bad[:1000],
              "unreadable_sector_count": len(bad), "kernel_readonly_flag": ro,
              "write_blocker_note": "Hardware write-blocker required for original media; "
                                    "software read-only is advisory."}
    ledger.append("acquisition-complete", **{k: v for k, v in result.items() if k != "unreadable_sectors"})
    return result
