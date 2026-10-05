"""Common evidence model every vendor plugin returns."""
from dataclasses import dataclass, field
from enum import IntEnum


class EvidenceLevel(IntEnum):
    """How strongly a recovered frame is tied to its time and camera."""
    CONTENT_ONLY = 1        # video bytes only; camera/time from content analysis or unknown
    CARVED_TIMESTAMPED = 2  # carved, with a recorder-written timestamp in the container
    INDEX_CONFIRMED = 3     # listed in the recorder's own live index

    @property
    def label(self) -> str:
        return {1: "content-only", 2: "carved + timestamp", 3: "index-confirmed"}[self.value]


@dataclass
class Frame:
    offset: int                     # container frame start in the source
    length: int                     # container frame length
    es: tuple                       # ((offset, length), ...) H.264 payload ranges in the source
    keyframe: bool
    family: str
    level: EvidenceLevel
    channel: int | None = None      # 1-based
    time_ms: int | None = None      # recorder time, ms since epoch (UTC basis)
    time_source: str = "none"       # index | container | ofni | overlay | none
    pts_90k: int | None = None
    label: str | None = None        # burned-in camera name, if read


@dataclass
class Finding:
    kind: str                       # e.g. format-event, index-reset, overwrite, clock-drift, gap
    severity: str                   # info | warning | critical
    summary: str
    detail: dict = field(default_factory=dict)


@dataclass
class Recovery:
    family: str
    plugin: str
    frames: list[Frame] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    fragments: list[tuple[int, int]] = field(default_factory=list)  # undecodable remnants
