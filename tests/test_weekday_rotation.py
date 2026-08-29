"""Deterministic coverage for weekday source selection and one-image invariant."""
from __future__ import annotations

import io
import json
from datetime import date

import pytest
from PIL import Image as PILImage

from adapters.image_sources.temples import (
    ConfiguredTempleSource,
    IskconBangaloreSource,
    IskconTirupatiSource,
    IskconVrindavanSource,
    MahakalSource,
    MayapurSource,
    SalangpurSource,
    SwaminarayanSource,
)
from application.image_service import ImageCollector, ImageService
from application.image_service import AllSourcesFailed
from domain.image import Image
from scheduler import run_image, run_pages
from config import Container
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


def test_every_enabled_configured_temple_is_registered_as_a_source():
    container = Container.__new__(Container)
    container.config = {
        "daily_image_rotation": {"saturday": ["known", "new_temple"]},
        "temple_sources": {
            "known": {"page_url": "https://known.test/darshan", "enabled": True},
            "new_temple": {"page_url": "https://new.test/darshan", "enabled": True},
            "disabled": {"page_url": "https://disabled.test/darshan", "enabled": False},
        },
        "image_source_config": {},
    }

    sources = container._build_sources()

    assert set(sources) == {"known", "new_temple"}
    assert isinstance(sources["new_temple"], ConfiguredTempleSource)
    assert sources["new_temple"].name == "new_temple"


def test_salangpur_uses_requested_date_and_ignores_logo():
    on_date = date(2026, 8, 25)
    session = Session([Response('<img src="/logo.png" alt="logo"><img src="/today.jpg" alt="Kashtbhanjan Hanuman darshan">'), Response(content=b"ok")])
    source = SalangpurSource("https://example.test/dev.php", session=session)
    image = source.fetch(on_date)
    assert "date=2026-08-25" in session.calls[0]
    assert source.last_image_url == "https://example.test/today.jpg"
    assert image and image.source == "salangpur"


@pytest.mark.parametrize("cls, html, expected", [
    (IskconBangaloreSource, '<time>2026-08-24</time><img src="krishna.jpg" alt="Krishna darshan">', False),
    (IskconBangaloreSource, '<time>2026-08-25</time><img src="krishna.jpg" alt="Krishna darshan">', True),
])
def test_dated_pages_reject_stale_content(cls, html, expected):
    session = Session([Response(html), Response(content=b"ok")])
    source = cls("https://example.test/darshan", session=session)
    assert bool(source.fetch(date(2026, 8, 25))) is expected


def test_iskcon_bangalore_uses_wordpress_original_image_url():
    on_date = date(2026, 8, 26)
    session = Session([
        Response('<time>2026-08-26</time><img src="/wp-content/uploads/2026/08/darshan-260x325.jpeg" alt="Krishna darshan">'),
        Response(content=b"original"),
    ])
    source = IskconBangaloreSource("https://example.test/daily-darshan", session=session)

    assert source.fetch(on_date)
    assert session.calls[1] == "https://example.test/wp-content/uploads/2026/08/darshan.jpeg"
    assert source.last_image_url == session.calls[1]


def test_iskcon_vrindavan_uses_dated_gallery_hydration_image():
    on_date = date(2026, 8, 26)
    session = Session([
        Response('window.__remixContext.enqueue("images_list [\\\"static/static-_16a8e9502b530a.jpg\\\"]")'),
        Response(content=b"gallery-image"),
    ])
    source = IskconVrindavanSource("https://iskconvrindavan.com/daily-darshan-gallery", session=session)

    image = source.fetch(on_date)

    assert image and image.source == "iskcon_vrindavan"
    assert session.calls == [
        "https://iskconvrindavan.com/daily-darshan-gallery/2026-08-26/2/sringar-darshan",
        "https://cdn.iskconvrindavan.com/static/static-_16a8e9502b530a.jpg",
    ]


def test_salangpur_prefers_largest_declared_darshan_image():
    on_date = date(2026, 8, 27)
    session = Session([
        Response(
            '<img src="logo.webp" alt="Temple logo" width="600" height="600">'
            '<img src="small.jpg" alt="Daily Darshan (27-08-2026 Thursday)" width="600" height="800">'
            '<img src="best.jpg" alt="Daily Darshan (27-08-2026 Thursday)" width="1600" height="2200">'
        ),
        Response(content=b"best-image"),
    ])
    source = SalangpurSource("https://example.test/dev-darshan", session=session)

    assert source.fetch(on_date)
    assert session.calls[1] == "https://example.test/best.jpg"


def test_tirupati_rejects_map_image_when_no_darshan_is_available():
    session = Session([Response('<img src="/wp-content/uploads/map_img.jpg" alt="Map Image">')])
    source = IskconTirupatiSource("https://example.test/daily-darshan", session=session)

    assert source.fetch(date(2026, 8, 27)) is None
    assert len(session.calls) == 1


def test_swaminarayan_uses_current_kalupur_card_to_fetch_largest_hd_image():
    on_date = date(2026, 8, 27)
    temple_id = "kalupur-id"
    session = Session([
        Response(
            '<a href="/api/iframe/temple?mode=dark&id=stale"><div class="header-container">Wed, 26 August 2026</div>'
            '<img data-src="https://images.example.test/stale.jpg"><div class="temple-name">Ahmedabad (Kalupur)</div>'
            '</a><a href="/api/iframe/temple?mode=dark&id=kalupur-id"><div class="header-container">Thu, 27 August 2026</div>'
            '<img data-src="https://images.example.test/cover.jpg"><div class="temple-name">Ahmedabad (Kalupur)</div></a>'
        ),
        Response(text=json.dumps(["https://images.example.test/small.jpg", "https://images.example.test/large.jpg"])),
        Response(content=_jpeg(800, 600)),
        Response(content=_jpeg(1600, 1200)),
    ])
    source = SwaminarayanSource("https://swaminarayan.info/daily-darshan", session=session)

    image = source.fetch(on_date)

    assert image and image.source == "swaminarayan"
    assert source.last_image_url == "https://images.example.test/large.jpg"
    assert session.calls == [
        "https://dailydarshanserver.nnd.media/api/iframe/content?mode=dark",
        f"https://dailydarshanserver.nnd.media/temple/dailydarshan/hd/{temple_id}/2026-08-27",
        "https://images.example.test/small.jpg",
        "https://images.example.test/large.jpg",
    ]


def _jpeg(width, height):
    buffer = io.BytesIO()
    PILImage.new("RGB", (width, height), "gold").save(buffer, format="JPEG")
    return buffer.getvalue()


def test_mahakal_uses_official_dated_full_size_image_and_selects_largest():
    on_date = date(2026, 8, 29)
    payload = {
        "success": True,
        "data": {
            "Bhasma Aarti": [
                {"media_type": "video", "date": "2026-08-29T00:00:00.000Z", "media_url": "https://media.test/video.mp4"},
                {"media_type": "image", "date": "2026-08-28T00:00:00.000Z", "actual_media_url": "https://media.test/stale.jpg"},
                {"media_type": "image", "date": "2026-08-29T00:00:00.000Z", "actual_media_url": "https://media.test/small.jpg", "thumbnail_url": "https://media.test/small-thumb.jpg"},
            ],
            "Bhog Aarti": [
                {"media_type": "image", "date": "2026-08-29T00:00:00.000Z", "actual_media_url": "https://media.test/large.jpg"},
            ],
        },
    }
    session = Session([
        Response(text=json.dumps(payload)),
        Response(content=_jpeg(800, 600)),
        Response(content=_jpeg(1600, 1200)),
    ])
    source = MahakalSource("https://example.test/live-darshan", session=session)

    image = source.fetch(on_date)

    assert image and image.source == "mahakal"
    assert source.last_image_url == "https://media.test/large.jpg"
    assert session.calls[0] == "https://prod-api.mahakal.brainabove.net/public/api/v1/media"
    assert session.calls[1:] == ["https://media.test/small.jpg", "https://media.test/large.jpg"]


def test_mayapur_uses_dated_album_original_and_selects_largest_image():
    on_date = date(2026, 8, 29)
    session = Session([
        Response(
            '<img alt="Daily Darshan" src="/storage/albums/old_cover.jpg"><p>28/08/2026</p><a href="/media/album/664">Show Album</a>'
            '<img alt="Daily Darshan" src="/storage/albums/today_cover.jpg"><p>29/08/2026</p><a href="/media/album/665">Show Album</a>'
        ),
        Response('<a href="/imageviewer/show-album-pictures/665/0"><img src="/storage/albums/665/small_thumbnail.jpg"></a>'),
        Response(
            'images[0]="/storage/albums/665/small_image.jpg"; '
            'images[1]="/storage/albums/665/large_image.jpg";'
        ),
        Response(content=_jpeg(800, 600)),
        Response(content=_jpeg(1800, 1200)),
    ])
    source = MayapurSource("https://www.mayapur.com/media/gallery/daily-darshan", session=session)

    image = source.fetch(on_date)

    assert image and image.source == "mayapur"
    assert source.last_image_url == "https://www.mayapur.com/storage/albums/665/large_image.jpg"
    assert session.calls == [
        "https://www.mayapur.com/media/gallery/daily-darshan",
        "https://www.mayapur.com/media/album/665",
        "https://www.mayapur.com/imageviewer/show-album-pictures/665/0",
        "https://www.mayapur.com/storage/albums/665/small_image.jpg",
        "https://www.mayapur.com/storage/albums/665/large_image.jpg",
    ]


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
        def collect_daily_images(self, *_): raise AllSourcesFailed("all remote failed")
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


def test_scheduler_stores_all_source_candidates_and_largest_canonical_image():
    on_date = date(2026, 8, 26)
    bangalore = Image(on_date, b"bangalore", "iskcon_bangalore")
    mayapur = Image(on_date, b"mayapur", "mayapur")

    class Images:
        def canonical_path(self, day): return f"docs/images/{day}.jpg"
        def candidate_path(self, day, source): return f"docs/images/{day}_{source}.jpg"
        def collect_daily_images(self, _day): return [bangalore, mayapur]
        def select_largest(self, _candidates): return mayapur
        def prune_images(self, **_): return []
    class Logs:
        def log(self, *_args, **_kwargs): pass
    class Pages:
        def write_all(self, *_args, **_kwargs): return []
    class Subs:
        def all(self): return []
    class Container:
        config = {"paths": {"images_dir": "docs/images"}, "delivery": {"image_retention_days": 7}}
        image_service, logs, page_renderer, subscribers = Images(), Logs(), Pages(), Subs()
        root = "."
    class Git:
        def __init__(self): self.writes = []
        def read_file(self, _path): return None
        def write_file(self, path, data, *_): self.writes.append((path, data))
        def commit(self, *_): pass

    git = Git()
    assert run_image(Container(), git, on_date) == 0
    assert git.writes == [
        ("docs/images/2026-08-26_iskcon_bangalore.jpg", b"bangalore"),
        ("docs/images/2026-08-26_mayapur.jpg", b"mayapur"),
        ("docs/images/2026-08-26.jpg", b"mayapur"),
    ]


def test_image_only_store_skips_subscriber_page_rendering():
    on_date = date(2026, 8, 26)
    candidate = Image(on_date, b"candidate", "mayapur")

    class Images:
        def canonical_path(self, day): return f"docs/images/{day}.jpg"
        def candidate_path(self, day, source): return f"docs/images/{day}_{source}.jpg"
        def collect_daily_images(self, _day): return [candidate]
        def select_largest(self, candidates): return candidates[0]
        def prune_images(self, **_): return []
    class Logs:
        def log(self, *_args, **_kwargs): pass
    class Pages:
        def __init__(self): self.calls = 0
        def write_all(self, *_args, **_kwargs): self.calls += 1; return []
    class Subs:
        def all(self): return []
    pages = Pages()
    class Container:
        config = {"paths": {"images_dir": "docs/images"}, "delivery": {"image_retention_days": 7}}
        image_service, logs, page_renderer, subscribers = Images(), Logs(), pages, Subs()
        root = "."
    class Git:
        def read_file(self, _path): return None
        def write_file(self, *_args): pass
        def commit(self, *_args): pass

    assert run_image(Container(), Git(), on_date, render_pages=False) == 0
    assert pages.calls == 0


def test_pages_job_uses_stored_image_without_fetching_sources():
    on_date = date(2026, 8, 26)

    class Images:
        def canonical_path(self, day): return f"docs/images/{day}.jpg"
    class Validator:
        def validate(self, image): return image.data == b"stored-image"
    class Logs:
        def __init__(self): self.events = []
        def log(self, *args, **_kwargs): self.events.append(args)
    class Pages:
        def __init__(self): self.calls = 0
        def write_all(self, *_args, **_kwargs):
            self.calls += 1
            return ["docs/subscriber/index.html"]
    class Subs:
        def all(self): return []
    class Container:
        config = {"paths": {"images_dir": "docs/images", "logs_csv": "csv/logs.csv"}}
        image_service, image_validator, logs, page_renderer, subscribers = Images(), Validator(), Logs(), Pages(), Subs()
        root = "."
    class Git:
        def __init__(self): self.commits = []
        def read_file(self, path): return b"stored-image" if path.endswith(".jpg") else None
        def commit(self, paths, *_): self.commits.append(paths)

    git = Git()
    container = Container()
    assert run_pages(container, git, on_date) == 0
    assert container.page_renderer.calls == 1
    assert git.commits == [["docs/subscriber/index.html", "csv/logs.csv"]]


def test_pages_job_fails_when_todays_image_is_missing():
    on_date = date(2026, 8, 26)

    class Images:
        def canonical_path(self, day): return f"docs/images/{day}.jpg"
    class Validator:
        def validate(self, _image): return False
    class Container:
        config = {"paths": {"images_dir": "docs/images"}}
        image_service, image_validator = Images(), Validator()
    class Git:
        def read_file(self, _path): return None

    assert run_pages(Container(), Git(), on_date) == 1
