from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from adapters.page_renderer import PageRenderer
from application.events import current_menu_event, daily_menu_shloka_available, event_for_date
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


def test_normal_daily_menu_shloka_opens_at_six_am_ist_only():
    config = {"timezone": "Asia/Kolkata", "available_from": "06:00"}
    before = datetime(2026, 9, 22, 5, 59, tzinfo=ZoneInfo("Asia/Kolkata"))
    released = datetime(2026, 9, 22, 6, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert daily_menu_shloka_available(config, before) is False
    assert daily_menu_shloka_available(config, released) is True


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


def test_normal_daily_page_shows_selected_source_shloka():
    renderer = PageRenderer(
        image_public_base="https://vipseva.com",
        daily_shlokas={"mahakal": "ॐ नमः शिवाय।", "default": "Neutral"},
    )

    page = renderer.render_html(
        Subscriber("9199", "monthly", subscription_id="opaque"),
        date(2026, 10, 12), delivered=True, source="mahakal",
    )

    assert "Mahakal · <span data-shloka-label>Today's Shloka</span>" in page
    assert "ॐ नमः शिवाय।" in page
    assert 'data-shloka-date="2026-10-12"' in page
    assert 'label.textContent = "Yesterday\'s Shloka"' in page


def test_event_shloka_has_priority_over_selected_source_shloka():
    renderer = PageRenderer(
        image_public_base="https://vipseva.com", events=EVENTS,
        daily_shlokas={"mahakal": "ॐ नमः शिवाय।", "default": "Neutral"},
    )

    page = renderer.render_html(
        Subscriber("9199", "monthly", subscription_id="opaque"),
        date(2026, 10, 11), delivered=True, source="mahakal",
    )

    assert "Maa Shailaputri" in page
    assert "ॐ देवी शैलपुत्र्यै नमः।" in page
    assert "ॐ नमः शिवाय।" not in page
    assert 'data-shloka-label>Today\'s Shloka</p>' in page


def test_menu_shows_todays_approved_mahakal_shloka(monkeypatch, container):
    import main
    today = date(2026, 9, 21)
    monkeypatch.setattr(main, "today_ist", lambda: today)
    monkeypatch.setattr(main, "current_menu_event", lambda events: None)
    monkeypatch.setattr(main, "daily_menu_shloka_available", lambda config: True)
    container.config["daily_shlokas"] = {"mahakal": "ॐ नमः शिवाय।"}
    container.image_reviews.upsert("mahakal-approved", {
        "id": "mahakal-approved", "date": today.isoformat(), "generation": "batch",
        "source": "mahakal", "path": "docs/images/mahakal.jpg", "sha256": "x",
        "status": "APPROVED", "approved_by": "admin", "approved_at": "now",
    })
    container.whatsapp = FakeWhatsApp()

    main._send_menu(container, "9199")

    assert "Mahakal · Today's Shloka" in container.whatsapp.sent[-1]["body"]
    assert "ॐ नमः शिवाय।" in container.whatsapp.sent[-1]["body"]
    assert "Today's Darshan is ready." not in container.whatsapp.sent[-1]["body"]


def test_menu_uses_fallback_shloka_after_six_until_todays_source_is_approved(monkeypatch, container):
    import main
    monkeypatch.setattr(main, "current_menu_event", lambda events: None)
    monkeypatch.setattr(main, "daily_menu_shloka_available", lambda config: True)
    container.config["daily_shlokas"] = {
        "fallback": "ॐ सर्वे भवन्तु सुखिनः। सर्वे सन्तु निरामयाः॥"
    }
    container.whatsapp = FakeWhatsApp()

    main._send_menu(container, "9199")

    body = container.whatsapp.sent[-1]["body"]
    assert "🌺 Today's Shloka" in body
    assert "ॐ सर्वे भवन्तु सुखिनः। सर्वे सन्तु निरामयाः॥" in body
    assert "Mahakal" not in body


def test_menu_retains_previous_approved_source_before_six_am(monkeypatch, container):
    import main
    today = date(2026, 9, 22)
    monkeypatch.setattr(main, "today_ist", lambda: today)
    monkeypatch.setattr(main, "current_menu_event", lambda events: None)
    monkeypatch.setattr(main, "daily_menu_shloka_available", lambda config: False)
    container.config["daily_shlokas"] = {
        "mahakal": "ॐ नमः शिवाय।", "fallback": "Neutral"
    }
    container.image_reviews.upsert("yesterday-mahakal", {
        "id": "yesterday-mahakal", "date": "2026-09-21", "generation": "batch",
        "source": "mahakal", "path": "docs/images/mahakal.jpg", "sha256": "x",
        "status": "APPROVED", "approved_by": "admin", "approved_at": "now",
    })
    container.whatsapp = FakeWhatsApp()

    main._send_menu(container, "9199")

    body = container.whatsapp.sent[-1]["body"]
    assert "Mahakal · Yesterday's Shloka" in body
    assert "ॐ नमः शिवाय।" in body


def test_menu_retains_previous_event_shloka_before_six_am(monkeypatch, container):
    import main
    today = date(2026, 10, 12)
    monkeypatch.setattr(main, "today_ist", lambda: today)
    monkeypatch.setattr(main, "current_menu_event", lambda events: None)
    monkeypatch.setattr(main, "daily_menu_shloka_available", lambda config: False)
    container.config["events"] = EVENTS
    container.whatsapp = FakeWhatsApp()

    main._send_menu(container, "9199")

    assert "Maa Shailaputri" in container.whatsapp.sent[-1]["body"]


def test_event_menu_shloka_overrides_todays_approved_source(monkeypatch, container):
    import main
    today = date(2026, 10, 11)
    event = event_for_date(EVENTS, today)
    monkeypatch.setattr(main, "today_ist", lambda: today)
    monkeypatch.setattr(main, "current_menu_event", lambda events: event)
    monkeypatch.setattr(main, "daily_menu_shloka_available", lambda config: True)
    container.config["events"] = EVENTS
    container.config["daily_shlokas"] = {"mahakal": "ॐ नमः शिवाय।"}
    container.image_reviews.upsert("mahakal-approved", {
        "id": "mahakal-approved", "date": today.isoformat(), "generation": "batch",
        "source": "mahakal", "path": "docs/images/mahakal.jpg", "sha256": "x",
        "status": "APPROVED", "approved_by": "admin", "approved_at": "now",
    })
    container.whatsapp = FakeWhatsApp()

    main._send_menu(container, "9199")

    body = container.whatsapp.sent[-1]["body"]
    assert "ॐ देवी शैलपुत्र्यै नमः।" in body
    assert "ॐ नमः शिवाय।" not in body
