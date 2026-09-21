"""Pre-uploaded event artwork, offered through normal image review."""
from pathlib import Path

from domain.image import Image


class EventImageSource:
    def __init__(self, root: str, path: str, name: str):
        self.root = Path(root).resolve()
        self.path = path
        self.name = name

    def fetch(self, on_date):
        path = (self.root / self.path).resolve()
        if not path.is_relative_to(self.root / "assets" / "events"):
            raise ValueError("Custom event images must be inside assets/events")
        if not path.is_file():
            return None
        return Image(on_date, data=path.read_bytes(), source=self.name)
