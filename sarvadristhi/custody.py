"""Append-only, hash-chained chain-of-custody ledger (JSON Lines).

Each entry carries the SHA-256 of the previous entry; editing or deleting any line breaks
every later link, which verify() reports."""
import getpass
import hashlib
import json
import platform
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def _digest(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]

    def append(self, action: str, **details) -> dict:
        prev = self.entries()
        entry = {"seq": len(prev), "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "actor": getpass.getuser(), "host": platform.node(), "action": action,
                 "details": details, "prev": prev[-1]["hash"] if prev else GENESIS}
        entry["hash"] = _digest(entry)
        with self.path.open("a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def verify(self) -> tuple[bool, str]:
        prev = GENESIS
        for i, e in enumerate(self.entries()):
            if e.get("seq") != i:
                return False, f"entry {i}: sequence number {e.get('seq')} out of order"
            if e.get("prev") != prev:
                return False, f"entry {i}: link to previous entry broken"
            if _digest(e) != e.get("hash"):
                return False, f"entry {i}: content altered (hash mismatch)"
            prev = e["hash"]
        return True, f"{len(self.entries())} entries, chain intact"
