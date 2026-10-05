"""Misleading-label evidence: a copy of a recorder image with another brand's string written into
unused space, as a rebadged or relabelled unit might carry. Detection must still follow the
structures on disk, and the workbench must show the label as a conflict, not obey it.

    python -m pramanshala.labels SRC.img DST.img ["CP PLUS DVR CP-UVR-0401E1"]"""
import shutil
import sys
from pathlib import Path

PROTECTED = [(0x1BE, 0x200), (0x200, 0x300)]   # MBR partition table, Hikvision master sector


def relabel(src: Path, dst: Path, text: bytes = b"CP PLUS DVR CP-UVR-0401E1-H") -> dict:
    src, dst = Path(src), Path(dst)
    if dst.exists():
        raise FileExistsError(dst)
    shutil.copyfile(src, dst)
    need = len(text) + 16
    with open(dst, "r+b") as f:
        head = f.read(1 << 20)
        off = 0x40
        while off + need <= len(head):
            clash = any(off < b and a < off + need for a, b in PROTECTED)
            if not clash and head[off:off + need] == b"\x00" * need:
                break
            off += 16
        else:
            raise ValueError("no unused space in the first MiB for a label")
        f.seek(off + 8)
        f.write(text)
    return {"image": str(dst), "label": text.decode(), "offset": off + 8, "source": str(src)}


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) < 2:
        raise SystemExit(__doc__)
    print(relabel(Path(a[0]), Path(a[1]), a[2].encode() if len(a) > 2 else b"CP PLUS DVR CP-UVR-0401E1-H"))
