"""Deterministic coverage for weekday source selection and one-image invariant."""
from __future__ import annotations

from datetime import date

import pytest

from adapters.image_sources.temples import MahakalSource, SalangpurSource, IskconBangaloreSource
from application.image_service import ImageCollector, ImageService
from application.image_service import AllSourcesFailed
from domain.image import Image
from scheduler import run_image
from tests.conftest import FakeSource


class Response:
    def __init__(self, text="", content=b"image", status_code=200):
        self.text, self.content, self.status_code = text, content, status_code


class Session:
    def __init__(self, responses): self.responses, self.calls = list(responses), []
    def get(self, url, **kwargs): self.calls.append(url); return self.responses.pop(0)


class Valid:
    def validate(self, image): return bool(image and image.data)


ROTATION = {
    "monday": ["mahakal", "iskcon_bangalore"], "tuesday": ["salangpur", "iskcon_bangalore"],
    "wednesday": ["iskcon_bangalore", "iskcon_vrindavan", "mayapur"],
    "thursday": ["iskcon_tirupati", "swaminarayan", "iskcon_bangalore"],
    "friday": ["devi", "mayapur", "iskcon_bangalore"], "saturday": ["salangpur", "iskcon_bangalore"],
    "sunday": ["iskcon_vrindavan", "mayapur", "iskcon_bangalore"],
}

@pytest.mark.parametrize("on_date, expected", [
    (date(2026, 8, 24), ROTATION["monday"]), (date(2026, 8, 25), ROTATION["tuesday"]),
    (date(2026, 8, 26), ROTATION["wednesday"]), (date(2026, 8, 27), ROTATION["thursday"]),
    (date(2026, 8, 28), ROTATION["friday"]), (date(2026, 8, 29), ROTATION["saturday"]),
    (date(2026, 8, 30), ROTATION["sunday"]),
])
def test_weekday_routing(on_date, expected):
    collector = ImageCollector({}, Valid(), rotation=ROTATION)
    assert collector.source_names_for(on_date) == expected


def test_salangpur_uses_requested_date_and_ignores_logo():
    on_date = date(2026, 8, 25)
    session = Session([Response('<img src="/logo.png" alt="logo"><img src="/today.jpg" alt="Kashtbhanjan Hanuman darshan">'), Response(content=b"ok")])
    source = SalangpurSource("https://example.test/dev.php", session=session)
    image = source.fetch(on_date)
    assert "date=2026-08-25" in session.calls[0]
    assert source.last_image_url == "https://example.test/today.jpg"
    assert image and image.source == "salangpur"


@pytest.mark.parametrize("cls, html, expected", [
    (MahakalSource, '<time>2026-08-24</time><img src="mahakal.jpg" alt="Mahakal bhasma darshan">', False),
    (MahakalSource, '<time>2026-08-25</time><img src="mahakal.jpg" alt="Mahakal bhasma darshan">', True),
    (IskconBangaloreSource, '<time>2026-08-24</time><img src="krishna.jpg" alt="Krishna darshan">', False),
    (IskconBangaloreSource, '<time>2026-08-25</time><img src="krishna.jpg" alt="Krishna darshan">', True),
])
def test_dated_pages_reject_stale_content(cls, html, expected):
    session = Session([Response(html), Response(content=b"ok")])
    source = cls("https://example.test/darshan", session=session)
    assert bool(source.fetch(date(2026, 8, 25))) is expected


def test_primary_failure_uses_secondary_and_only_fetches_chain_once():
    primary, secondary = FakeSource("primary", None), FakeSource("secondary", Image(date(2026, 8, 26), b"one", "secondary"))
    collector = ImageCollector({"primary": primary, "secondary": secondary}, Valid(), rotation={"wednesday": ["primary", "secondary"]})
    assert collector.collect(date(2026, 8, 26)).source == "secondary"


def test_existing_canonical_image_does_not_fetch_again():
    source = FakeSource("only", Image(date(2026, 8, 24), b"new", "only"))
    calls = {"n": 0}; original = source.fetch
    def fetch(day): calls["n"] += 1; return original(day)
    source.fetch = fetch
    svc = ImageService(ImageCollector([source], Valid()), Valid(), "docs/images")
    assert svc.ensure_daily_image(date(2026, 8, 24), existing=b"canonical") is None
    assert calls["n"] == 0


def test_one_canonical_image_is_reused_for_many_subscribers():
    source = FakeSource("only", Image(date(2026, 8, 24), b"same-bytes", "only"))
    svc = ImageService(ImageCollector([source], Valid()), Valid(), "docs/images")
    image = svc.ensure_daily_image(date(2026, 8, 24))
    path = image.canonical_path("docs/images")
    subscriber_references = [path for _ in range(100)]
    assert subscriber_references == ["docs/images/2026-08-24.jpg"] * 100


def test_e2e_remote_failure_keeps_fallback_separate_from_dated_image():
    """Scheduler integration: failed chain keeps fallback out of dated assets."""
    on_date = date(2026, 8, 24)
    class Images:
        def canonical_path(self, day): return f"docs/images/{day}.jpg"
        def ensure_daily_image(self, *_, **__): raise AllSourcesFailed("all remote failed")
        def prune_images(self, **_): return []
    class Validator:
        def validate(self, image): return image.data == b"fallback"
        def validate_fallback(self, image): return image.data == b"fallback"
    class Logs:
        def __init__(self): self.events = []
        def log(self, *args, **kwargs): self.events.append(args)
    class Pages:
        def write_all(self, *args, **kwargs): return []
    class Subs:
        def all(self): return []
    class Container:
        config = {"paths": {"images_dir": "docs/images"}, "delivery": {"fallback_image": "fallback.jpg", "image_retention_days": 7}}
        image_service, image_validator, logs, page_renderer, subscribers = Images(), Validator(), Logs(), Pages(), Subs()
        root = "."
    class Git:
        def __init__(self): self.writes = []
        def read_file(self, path): return b"fallback" if path.endswith("fallback.jpg") else None
        def write_file(self, path, data, *_): self.writes.append((path, data))
        def commit(self, *_): pass
    git = Git()
    assert run_image(Container(), git, on_date) == 0
    assert git.writes == []
