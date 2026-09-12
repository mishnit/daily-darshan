from datetime import date
from urllib.parse import quote

from adapters.page_renderer import PageRenderer
from domain.subscriber import Subscriber


def test_share_cta_contains_subscriber_referrer():
    html = PageRenderer(image_public_base="https://vipseva.com").render_html(
        Subscriber("919535507255", "monthly", subscription_id="opaque"),
        date(2026, 9, 12), delivered=True,
        image_name="image.jpg",
    )
    share = (
        "Radhe Radhe 🙏\n\nToday's HD Daily Darshan:\n"
        "https://vipseva.com/images/image.jpg"
        "\n\nVisit VIP Seva for daily darshan:\nhttps://vipseva.com/?ref=919535507255"
    )
    assert f"https://wa.me/?text={quote(share, safe='')}" in html


def test_vip_landing_pages_preserve_referrer_in_whatsapp_link():
    for path in ("docs/index.html", "docs/images/index.html"):
        html = open(path, encoding="utf-8").read()
        assert "URLSearchParams(window.location.search).get(\"ref\")" in html
        assert "Radhe Radhe ref=\" + ref" in html


def test_referral_recorded_once_per_processed_message(tmp_path):
    from repositories.csv_repository import CSVRepository
    repo = CSVRepository(str(tmp_path / "referrals.csv"),
                         ["message_id", "visitor_mobile", "referrer_mobile", "recorded_at"],
                         "message_id")
    row = {"message_id": "m1", "visitor_mobile": "9199",
           "referrer_mobile": "919535507255", "recorded_at": "now"}
    repo.upsert("m1", row)
    repo.upsert("m1", row)
    assert len(repo.all()) == 1
