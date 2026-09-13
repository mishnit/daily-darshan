from datetime import date
from types import SimpleNamespace

import pytest

from scheduler import run_pages


@pytest.mark.parametrize("source", ["canonical", "iskcon_mumbai"])
@pytest.mark.parametrize("today_valid", [True, False])
def test_pages_prefers_today_then_latest_valid_same_source(tmp_path, source, today_valid):
    day = date(2026, 9, 13)
    suffix = "" if source == "canonical" else "_" + source
    directory = tmp_path / "docs/images"
    directory.mkdir(parents=True)
    names = {
        f"2026-09-13{suffix}.jpg": b"valid" if today_valid else b"broken",
        f"2026-09-12{suffix}.jpg": b"broken",
        f"12345678-1234-1234-1234-123456789abc_2026-09-11{suffix}.jpg": b"valid",
        f"2026-09-10{suffix}.jpg": b"valid",
        f"2026-09-14{suffix}.jpg": b"valid",
        "2026-09-12_other_source.jpg": b"valid",
    }
    for name, data in names.items():
        (directory / name).write_bytes(data)
    calls, commits = [], []
    c = SimpleNamespace(root=str(tmp_path), config={"paths": {"images_dir": "docs/images"}},
        image_service=SimpleNamespace(
            canonical_path=lambda d, **kw: f"docs/images/{d}.jpg",
            candidate_path=lambda d, s, **kw: f"docs/images/{d}_{s}.jpg"),
        image_validator=SimpleNamespace(validate=lambda img: img.data == b"valid"),
        subscribers=SimpleNamespace(all=lambda: []),
        page_renderer=SimpleNamespace(write_all=lambda *a, **kw: calls.append((a, kw)) or ["docs/sub/index.html"]),
        logs=SimpleNamespace(log=lambda *a, **kw: None))
    git = SimpleNamespace(read_file=lambda p: (tmp_path / p).read_bytes() if (tmp_path / p).exists() else None,
                          commit=lambda *a: commits.append(a))
    assert run_pages(c, git, day, source) == 0
    expected = f"2026-09-13{suffix}.jpg" if today_valid else f"12345678-1234-1234-1234-123456789abc_2026-09-11{suffix}.jpg"
    assert calls[0][1]["image_name"] == expected
    assert calls[0][0][1] == day
    assert len(commits) == 1
