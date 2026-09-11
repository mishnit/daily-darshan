"""Section 20: image validation and source fallback."""
from __future__ import annotations

import io
import os
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest

from domain.image import Image
from application.image_service import ImageCollector, ImageService, AllSourcesFailed
from adapters.image_sources.validator import ImageValidator
from tests.conftest import FakeSource

try:
    from PIL import Image as PILImage
    _PIL = True
except Exception:
    _PIL = False


def _png_bytes(w=300, h=300) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (w, h), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_validator_rejects_empty():
    v = ImageValidator(min_width=0, min_height=0)
    assert v.validate(Image(image_date=date.today(), data=b"")) is False


@pytest.mark.skipif(not _PIL, reason="Pillow not installed")
def test_validator_accepts_valid_png():
    v = ImageValidator(allowed_formats=["PNG"], min_width=200, min_height=200)
    img = Image(image_date=date.today(), data=_png_bytes())
    assert v.validate(img) is True


@pytest.mark.skipif(not _PIL, reason="Pillow not installed")
def test_validator_rejects_small_dimensions():
    v = ImageValidator(allowed_formats=["PNG"], min_width=500, min_height=500)
    img = Image(image_date=date.today(), data=_png_bytes(100, 100))
    assert v.validate(img) is False


@pytest.mark.skipif(not _PIL, reason="Pillow not installed")
def test_validator_accepts_small_valid_fallback():
    v = ImageValidator(allowed_formats=["PNG"], min_width=1080, min_height=1080)
    img = Image(image_date=date.today(), data=_png_bytes(612, 612))
    assert v.validate(img) is False
    assert v.validate_fallback(img) is True


@pytest.mark.skipif(not _PIL, reason="Pillow not installed")
def test_validator_rejects_garbage_bytes():
    v = ImageValidator(allowed_formats=["PNG", "JPEG"])
    img = Image(image_date=date.today(), data=b"not-an-image")
    assert v.validate(img) is False


def test_sha256_stable():
    img = Image(image_date=date.today(), data=b"abc")
    assert img.sha256() == img.sha256()


# ------------------------- source fallback ------------------------- #

class AllValidValidator:
    def validate(self, image):
        return image is not None and not image.is_empty


def test_collector_uses_first_valid_source():
    good = Image(image_date=date.today(), data=b"good", source="temple")
    collector = ImageCollector([FakeSource("temple", good)], AllValidValidator())
    result = collector.collect(date.today())
    assert result.source == "temple"


def test_collector_falls_back_to_next_source():
    good = Image(image_date=date.today(), data=b"good", source="rss")
    collector = ImageCollector(
        [FakeSource("temple", None), FakeSource("rss", good)], AllValidValidator()
    )
    result = collector.collect(date.today())
    assert result.source == "rss"


def test_collector_returns_all_valid_source_candidates():
    first = Image(image_date=date.today(), data=b"one", source="first")
    second = Image(image_date=date.today(), data=b"two", source="second")
    collector = ImageCollector(
        [FakeSource("first", first), FakeSource("second", second)], AllValidValidator()
    )
    assert collector.collect_candidates(date.today()) == [first, second]


@pytest.mark.skipif(not _PIL, reason="Pillow not installed")
def test_collector_selects_largest_valid_remote_image():
    small = Image(image_date=date.today(), data=_png_bytes(1080, 1080), source="primary")
    large = Image(image_date=date.today(), data=_png_bytes(1080, 1350), source="secondary")
    validator = ImageValidator(allowed_formats=["PNG"], min_width=1080, min_height=1080)
    collector = ImageCollector(
        [FakeSource("primary", small), FakeSource("secondary", large)], validator
    )
    assert collector.collect(date.today()).source == "secondary"


def test_collector_skips_source_that_raises():
    good = Image(image_date=date.today(), data=b"good", source="website")
    collector = ImageCollector(
        [FakeSource("temple", raises=True), FakeSource("website", good)], AllValidValidator()
    )
    assert collector.collect(date.today()).source == "website"


def test_collector_all_fail_raises():
    collector = ImageCollector(
        [FakeSource("temple", None), FakeSource("rss", None)], AllValidValidator()
    )
    with pytest.raises(AllSourcesFailed):
        collector.collect(date.today())


def test_image_service_idempotent_when_existing_valid():
    good = Image(image_date=date.today(), data=b"new", source="temple")
    collector = ImageCollector([FakeSource("temple", good)], AllValidValidator())
    svc = ImageService(collector, AllValidValidator())
    # Existing valid bytes present -> returns None (no replacement, section 10).
    assert svc.ensure_daily_image(date.today(), existing=b"already-here") is None


def test_image_service_collects_when_no_existing():
    good = Image(image_date=date.today(), data=b"new", source="temple")
    collector = ImageCollector([FakeSource("temple", good)], AllValidValidator())
    svc = ImageService(collector, AllValidValidator())
    result = svc.ensure_daily_image(date.today(), existing=None)
    assert result is not None and result.data == b"new"


def test_image_service_force_refreshes_existing_valid_image():
    replacement = Image(image_date=date.today(), data=b"new", source="temple")
    collector = ImageCollector([FakeSource("temple", replacement)], AllValidValidator())
    svc = ImageService(collector, AllValidValidator())
    result = svc.ensure_daily_image(date.today(), existing=b"already-here", force_refresh=True)
    assert result is replacement


def test_image_service_candidate_path_uses_source_name(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    service = ImageService(None, None, str(images))
    path = service.candidate_path(date(2026, 8, 26), "ISKCON Bangalore")
    prefix, suffix = os.path.basename(path).split("_", 1)
    assert UUID(prefix).version == 4
    assert suffix == "2026-08-26_iskcon_bangalore.jpg"

    Path(path).write_bytes(b"candidate")
    assert service.candidate_path(date(2026, 8, 26), "ISKCON Bangalore") == path
    assert service.candidate_path(
        date(2026, 8, 26), "iskcon_bangalore", create=False
    ) == path


def test_image_service_does_not_allocate_missing_candidate_for_page_selection(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    service = ImageService(None, None, str(images))

    path = service.candidate_path(date(2026, 8, 26), "iskcon_mumbai", create=False)

    assert path == str(images / "2026-08-26_iskcon_mumbai.jpg")
    assert not Path(path).exists()


def test_image_service_allocates_and_reuses_opaque_canonical_path(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    service = ImageService(None, None, str(images))

    allocated = service.canonical_path(date(2026, 8, 26))
    prefix, suffix = os.path.basename(allocated).split("_", 1)
    assert UUID(prefix).version == 4
    assert suffix == "2026-08-26.jpg"

    Path(allocated).write_bytes(b"daily-image")
    assert service.canonical_path(date(2026, 8, 26), create=False) == allocated


def test_image_service_keeps_legacy_canonical_path_readable(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    legacy = images / "2026-08-26.jpg"
    legacy.write_bytes(b"legacy")
    service = ImageService(None, None, str(images))

    assert service.canonical_path(date(2026, 8, 26), create=False) == str(legacy)
    migrated = service.canonical_path(date(2026, 8, 26), create=True)
    prefix, suffix = os.path.basename(migrated).split("_", 1)
    assert UUID(prefix).version == 4
    assert suffix == "2026-08-26.jpg"
    assert migrated != str(legacy)
