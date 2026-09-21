from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from adapters.page_renderer import PageRenderer
from application.events import current_menu_event, event_for_date
from domain.subscriber import Subscriber
from tests.conftest import FakeWhatsApp
from tests.test_admin import container


EVENTS = [{
    "id": "navratri", "name": "Navratri", "enabled": True,
    "timezone": "Asia/Kolkata", "menu_available_from": "06:00",
    "menu_enabled": True, "page_enabled": True,
    "days": [{"date": "2026-10-11", "day_number": 1, "tithi": "Pratipada",
              "colour": "Orange", "deity": "Maa Shailaputri",
              "shloka": "ॐ देवी शैलपुत्र्यै नमः।"}],
}]


def test_event_menu_is_released_at_six_am_ist_only():
    before = datetime(2026, 10, 11, 5, 59, tzinfo=ZoneInfo("Asia/Kolkata"))
    released = datetime(2026, 10, 11, 6, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert current_menu_event(EVENTS, before) is None
    assert current_menu_event(EVENTS, released)["deity"] == "Maa Shailaputri"
    assert current_menu_event(EVENTS, released + timedelta(days=1)) is None


def test_config_has_nine_unique_navratri_days_with_shlokas():
    import json
    config = json.loads(open("config.json", encoding="utf-8").read())
    days = config["events"][0]["days"]
    assert len(days) == 9
    assert len({day["date"] for day in days}) == 9
    assert all(day["shloka"] for day in days)


def test_menu_body_includes_todays_event_without_separate_cta(monkeypatch, container):
    import main
    today = event_for_date(EVENTS, date(2026, 10, 11))
    monkeypatch.setattr(main, "current_menu_event", lambda events: today)
    container.config["events"] = EVENTS
    container.whatsapp = FakeWhatsApp()

    main._send_menu(container, "9199")

    sent = container.whatsapp.sent[-1]
    assert "Maa Shailaputri" in sent["body"]
    assert "Orange" in sent["body"]
    assert "ॐ देवी शैलपुत्र्यै नमः।" in sent["body"]
    assert not any(row.startswith("EVENT_") for row in sent["rows"])


def test_active_subscriber_menu_includes_personalised_page_without_extra_message(monkeypatch, container):
    import main
    today = event_for_date(EVENTS, date(2026, 10, 11))
    monkeypatch.setattr(main, "current_menu_event", lambda events: today)
    container.config["events"] = EVENTS
    container.config["delivery"]["page_base_url"] = "https://vipseva.com"
    container.subscriber_service.upsert_pending("9199", "monthly", "Nitin")
    container.subscriber_service.grant_opt_in("9199", "test")
    container.subscriber_service.activate("9199")
    subscription_id = container.subscribers.find("9199").subscription_id
    container.whatsapp = FakeWhatsApp()

    main._send_menu(container, "9199")

    sent = container.whatsapp.sent
    assert len(sent) == 1
    assert sent[0]["type"] == "list"
    assert "Maa Shailaputri" in sent[0]["body"]
    assert f"https://vipseva.com/{subscription_id}" in sent[0]["body"]


def test_subscription_page_shows_matching_event_only():
    renderer = PageRenderer(image_public_base="https://vipseva.com", events=EVENTS)
    subscriber = Subscriber("9199", "monthly", subscription_id="opaque")

    festival_page = renderer.render_html(subscriber, date(2026, 10, 11), delivered=True)
    ordinary_page = renderer.render_html(subscriber, date(2026, 10, 12), delivered=True)

    assert "Maa Shailaputri" in festival_page
    assert "Pratipada · Orange" in festival_page
    assert "ॐ देवी शैलपुत्र्यै नमः।" in festival_page
    assert "Maa Shailaputri" not in ordinary_page
