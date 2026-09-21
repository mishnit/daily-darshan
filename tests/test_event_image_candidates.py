from datetime import date
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image as PILImage

from adapters.image_sources.event import EventImageSource
from adapters.image_sources.validator import ImageValidator
from application.image_service import ImageCollector
from application.image_approval import collect_for_review, approved
from domain.image import Image
from tests.test_admin import container


DAY = date(2026, 10, 11)
PATH = 'assets/events/sharad-navratri-2026/01-shailaputri.jpg'


def jpeg(size=(640, 800)):
    out = BytesIO()
    PILImage.new('RGB', size, 'orange').save(out, format='JPEG')
    return out.getvalue()


def test_event_candidate_joins_temple_review_without_auto_approval(container, tmp_path):
    path = tmp_path / PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(jpeg())
    container.config['events'] = [{'id': 'navratri', 'days': [
        {'date': DAY.isoformat(), 'day_number': 1, 'deity': 'Maa Shailaputri', 'image_path': PATH}]}]
    temple = SimpleNamespace(name='temple', fetch=lambda day: Image(day, jpeg(), 'temple'))
    collector = ImageCollector({'temple': temple}, ImageValidator(min_width=600, min_height=600),
        rotation={'sunday': ['temple']}, event_sources=container._build_event_sources())
    assert [x.source for x in collector.collect_candidates(DAY)] == ['temple', 'Maa Shailaputri']
    assert [x.source for x in collector.collect_candidates(date(2026, 10, 18))] == ['temple']
    container.image_service._collector = collector
    writes = []
    git = SimpleNamespace(write_file=lambda *args: writes.append(args), commit=lambda *args: None)
    collect_for_review(container, git, DAY)
    rows = container.image_reviews.all()
    assert {r['source'] for r in rows} == {'temple', 'Maa Shailaputri'}
    assert all(r['status'] == 'PENDING' for r in rows)
    assert approved(container, DAY) is None
    assert len(writes) == 2
    event_row = next(r for r in rows if r['source'] == 'Maa Shailaputri')
    from scheduler import _watermark_details
    assert _watermark_details(DAY, event_row['source']) == 'Date: 2026-10-11 · Source: Maa Shailaputri'
    stored = {args[0]: args[1] for args in writes}
    # Save and reopen both generated previews, as the repository writer does.
    temple_row = next(r for r in rows if r['source'] == 'temple')
    for row in (temple_row, event_row):
        saved = tmp_path / row['path']
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(stored[row['path']])
    with PILImage.open(tmp_path / temple_row['path']) as normal:
        assert normal.format == 'JPEG'
        assert normal.size == (640, 800)
        assert normal.getpixel((20, 500))[0] > 240
        assert normal.getpixel((20, 780)) == (96, 96, 96)
    with PILImage.open(tmp_path / event_row['path']) as custom:
        assert custom.format == 'JPEG'
        assert custom.size == (640, 941)
        assert custom.getpixel((20, 780))[0] > 240
        assert custom.getpixel((20, 900)) == (96, 96, 96)
    with PILImage.open(BytesIO(stored[event_row['path']])) as preview:
        assert preview.size == (640, 941)
        # The original bottom remains orange, above the new grey footer.
        assert preview.getpixel((20, 780))[0] > 240
        assert preview.getpixel((20, 900)) == (96, 96, 96)
    assert path.read_bytes() == jpeg()
    event_row['status'] = 'APPROVED'
    container.image_reviews.upsert(event_row['id'], event_row)
    git.read_file = stored.get
    from application.image_approval import materialize
    materialize(container, git, DAY)
    assert writes[-1][1] == stored[event_row['path']]


@pytest.mark.parametrize('data', [None, b'broken', jpeg((599, 800))])
def test_unusable_custom_image_does_not_block_temple_candidate(tmp_path, data):
    path = tmp_path / PATH
    path.parent.mkdir(parents=True)
    if data is not None:
        path.write_bytes(data)
    temple = SimpleNamespace(name='temple', fetch=lambda day: Image(day, jpeg(), 'temple'))
    collector = ImageCollector([temple], ImageValidator(min_width=600, min_height=600),
        event_sources={DAY.isoformat(): [EventImageSource(str(tmp_path), PATH, 'event')]})
    assert [x.source for x in collector.collect_candidates(DAY)] == ['temple']


def test_disabled_event_has_no_custom_candidates(container):
    container.config['events'] = [{'enabled': False, 'days': [
        {'date': DAY.isoformat(), 'image_path': PATH}]}]
    assert container._build_event_sources() == {}


def test_custom_image_cannot_read_outside_event_assets(tmp_path):
    source = EventImageSource(str(tmp_path), '../../config.json', 'event')
    with pytest.raises(ValueError, match='assets/events'):
        source.fetch(DAY)


def test_images_disabled_preserves_event_content(container):
    from application.events import event_for_date, current_menu_event
    from datetime import datetime
    from zoneinfo import ZoneInfo
    event = {'id': 'navratri', 'enabled': True, 'images_enabled': False,
             'days': [{'date': DAY.isoformat(), 'day_number': 1,
                       'image_path': PATH, 'deity': 'Maa Shailaputri', 'shloka': 'Test shloka'}]}
    container.config['events'] = [event]
    assert container._build_event_sources() == {}
    assert event_for_date([event], DAY)['shloka'] == 'Test shloka'
    assert current_menu_event([event], datetime(2026, 10, 11, 7,
        tzinfo=ZoneInfo('Asia/Kolkata')))['shloka'] == 'Test shloka'
    event['images_enabled'] = True
    assert len(container._build_event_sources()[DAY.isoformat()]) == 1
    event['enabled'] = False
    assert container._build_event_sources() == {}
    assert event_for_date([event], DAY) is None


def test_force_recollect_supersedes_an_existing_approval(container, tmp_path):
    path = tmp_path / PATH
    path.parent.mkdir(parents=True)
    path.write_bytes(jpeg())
    container.config['events'] = [{'id': 'navratri', 'days': [
        {'date': DAY.isoformat(), 'day_number': 1, 'deity': 'Maa Shailaputri', 'image_path': PATH}]}]
    collector = ImageCollector(
        [SimpleNamespace(name='temple', fetch=lambda day: Image(day, jpeg(), 'temple'))],
        ImageValidator(min_width=600, min_height=600), event_sources=container._build_event_sources(),
    )
    container.image_service._collector = collector
    git = SimpleNamespace(write_file=lambda *args: None, commit=lambda *args: None)
    collect_for_review(container, git, DAY)
    chosen = container.image_reviews.all()[0]
    chosen['status'] = 'APPROVED'
    container.image_reviews.upsert(chosen['id'], chosen)

    collect_for_review(container, git, DAY, force_recollect=True)

    rows = container.image_reviews.all()
    assert len([row for row in rows if row['status'] == 'SUPERSEDED']) == 2
    assert len([row for row in rows if row['status'] == 'PENDING']) == 2
    assert approved(container, DAY) is None
