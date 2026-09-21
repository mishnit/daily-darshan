"""Date-aware festival content shared by WhatsApp and subscriber pages."""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo


def event_for_date(events: list[dict] | None, on_date: date) -> dict | None:
    """Return the enabled event-day configuration for ``on_date``."""
    wanted = on_date.isoformat()
    for event in events or []:
        if not event.get("enabled", True):
            continue
        for day in event.get("days", []):
            if day.get("date") == wanted:
                return {**day, "event_id": event.get("id", "event"),
                        "event_name": event.get("name", "Festival"),
                        "timezone": event.get("timezone", "Asia/Kolkata"),
                        "menu_available_from": event.get("menu_available_from", "06:00"),
                        "menu_enabled": event.get("menu_enabled", True),
                        "page_enabled": event.get("page_enabled", True)}
    return None


def current_menu_event(events: list[dict] | None, now: datetime | None = None) -> dict | None:
    """Return today's menu event once its local release time has arrived."""
    for event in events or []:
        if not event.get("enabled", True) or not event.get("menu_enabled", True):
            continue
        try:
            zone = ZoneInfo(event.get("timezone", "Asia/Kolkata"))
            local_now = now.astimezone(zone) if now else datetime.now(zone)
            hour, minute = (int(part) for part in event.get("menu_available_from", "06:00").split(":"))
            available_at = time(hour, minute)
        except (KeyError, TypeError, ValueError):
            continue
        day = event_for_date([event], local_now.date())
        if day and local_now.time().replace(tzinfo=None) >= available_at:
            return day
    return None


def event_message(day: dict, personalised_url: str = "") -> str:
    lines = [
        f"🙏 {day['event_name']} · Day {day['day_number']} ({day['tithi']})",
        f"🌺 {day['deity']}",
        f"🎨 Today's colour: {day['colour']}",
        "",
        str(day["shloka"]),
        "",
        f"May {day['deity']} bless you and your family. 🙏",
    ]
    if personalised_url:
        lines.extend(["", f"View today's Darshan on your personalised page: {personalised_url}"])
    return "\n".join(lines)


def source_shloka(daily_shlokas: dict[str, str] | None, source: str) -> tuple[str, str] | None:
    """Return a display name and shloka for an approved normal image source."""
    key = source.strip().lower().replace("-", "_")
    shloka = (daily_shlokas or {}).get(key) or (daily_shlokas or {}).get("default", "")
    if not shloka:
        return None
    words = key.split("_")
    title = " ".join(
        word.upper() if word == "iskcon" else word.title() for word in words if word
    ) or "Today's Darshan"
    return title, shloka


def source_shloka_message(source: str, daily_shlokas: dict[str, str] | None,
                          personalised_url: str = "") -> str:
    """Format the normal daily menu content after a source is approved."""
    selected = source_shloka(daily_shlokas, source)
    if not selected:
        return ""
    title, shloka = selected
    lines = [f"🌺 {title} · Today's Shloka", "", shloka]
    if personalised_url:
        lines.extend(["", f"View today's Darshan on your personalised page: {personalised_url}"])
    return "\n".join(lines)
