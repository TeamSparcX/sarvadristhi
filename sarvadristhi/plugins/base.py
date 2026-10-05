"""Plugin contract. Every vendor plugin maps its format onto the common Frame model."""
from ..models import Recovery
from ..source import Source


class Plugin:
    name = "base"
    family = "unknown"

    def detect(self, src: Source) -> tuple[float, dict]:
        """Return (confidence 0..1, evidence) that src holds this format family."""
        raise NotImplementedError

    def recover(self, src: Source) -> Recovery:
        """Parse any index, carve everything, return frames with evidence levels."""
        raise NotImplementedError
