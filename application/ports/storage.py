"""Image source and GitHub storage ports (Tech Doc sections 9, 10)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from domain.image import Image


class ImageSourcePort(ABC):
    """A single image source. fetch() returns a candidate or None."""

    name: str = "unknown"

    @abstractmethod
    def fetch(self, on_date: date) -> Image | None: ...


class GitHubRepositoryPort(ABC):
    @abstractmethod
    def read_file(self, path: str) -> bytes | None: ...

    @abstractmethod
    def write_file(self, path: str, content: bytes, message: str) -> None: ...

    @abstractmethod
    def commit(self, files: list[str], message: str) -> None: ...
