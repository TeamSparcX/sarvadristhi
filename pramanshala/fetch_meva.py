"""Download the MEVA clips used by the scenario (CC-BY-4.0, https://mevadata.org).

Parallel HTTP range requests against the public AWS bucket (no account needed). Each file is
verified against the bucket's Content-Length; the S3 ETag is recorded for provenance.

    python -m pramanshala.fetch_meva kit/meva"""
import concurrent.futures as cf
import json
import sys
import urllib.request
from pathlib import Path

BUCKET = "https://mevadata-public-01.s3.amazonaws.com"
CLIPS = [
    "drops-123-r13/2018-03-07/16/2018-03-07.16-50-01.16-55-01.school.G328.r13.avi",
    "drops-123-r13/2018-03-07/16/2018-03-07.16-50-06.16-55-06.school.G336.r13.avi",
    "drops-123-r13/2018-03-07/16/2018-03-07.16-50-01.16-55-01.admin.G326.r13.avi",
    "drops-123-r13/2018-03-07/16/2018-03-07.16-50-00.16-55-00.admin.G329.r13.avi",
    "drops-123-r13/2018-03-07/16/2018-03-07.16-50-00.16-55-00.bus.G331.r13.avi",
    "drops-123-r13/2018-03-07/16/2018-03-07.16-50-07.16-55-07.hospital.G301.r13.avi",
]
LABELS = "examples/annotations/2018-03-07/16/"
PART = 8 * 1024 * 1024


def _head(url):
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(r.headers["Content-Length"]), r.headers.get("ETag", "").strip('"')


def _range(url, a, b, retries=5):
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers={"Range": f"bytes={a}-{b}"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            if len(data) == b - a + 1:
                return a, data
        except OSError:
            if k == retries - 1:
                raise
    raise OSError(f"short read {url} {a}-{b}")


def fetch(key: str, out: Path, pool) -> dict:
    url = f"{BUCKET}/{key}"
    dst = out / Path(key).name
    size, etag = _head(url)
    if dst.exists() and dst.stat().st_size == size:
        return {"file": dst.name, "bytes": size, "etag": etag, "source": url, "cached": True}
    tmp = dst.with_suffix(".part")
    with open(tmp, "wb") as f:
        f.truncate(size)
    futs = [pool.submit(_range, url, a, min(size, a + PART) - 1) for a in range(0, size, PART)]
    with open(tmp, "r+b") as f:
        for fu in cf.as_completed(futs):
            a, data = fu.result()
            f.seek(a)
            f.write(data)
    tmp.replace(dst)
    return {"file": dst.name, "bytes": size, "etag": etag, "source": url}


def main(out_dir: str):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    got = []
    with cf.ThreadPoolExecutor(16) as pool:
        for key in CLIPS:
            r = fetch(key, out, pool)
            got.append(r)
            print(f"{r['file']}: {r['bytes'] / 1e6:.1f} MB", flush=True)
        # human activity labels for the same window (truth for scoring the AI)
        with urllib.request.urlopen(f"{BUCKET}/?list-type=2&prefix={LABELS}", timeout=60) as r:
            listing = r.read().decode()
        import re
        for key in re.findall(r"<Key>([^<]*\.activities\.yml)</Key>", listing):
            with urllib.request.urlopen(f"{BUCKET}/{key}", timeout=60) as r:
                (out / Path(key).name).write_bytes(r.read())
    try:
        with urllib.request.urlopen("http://mevadata.org/resources/MEVA-data-license.txt", timeout=60) as r:
            (out / "LICENSE-MEVA.txt").write_bytes(r.read())
    except OSError:
        pass
    (out / "SOURCES.json").write_text(json.dumps({
        "dataset": "MEVA (Multiview Extended Video with Activities), Kitware / IARPA DIVA",
        "license": "CC-BY-4.0", "home": "https://mevadata.org", "clips": got}, indent=2))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "kit/meva")
