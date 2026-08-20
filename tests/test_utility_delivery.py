"""Tests for the utility-template delivery transformation (8-change set)."""
from __future__ import annotations

from datetime import date

import pytest

from domain.enums import PaymentStatus, SubscriberStatus
from domain.payment import Payment
from domain.subscriber import Subscriber, new_subscription_id
from application.delivery_service import DeliveryService
from application.subscriber_service import SubscriberService
from adapters.page_renderer import PageRenderer
from tests.conftest import FakeWhatsApp


# ------------------------- #4 subscription_id ------------------------- #

def test_new_subscription_id_is_unguessable_and_unique():
    a, b = new_subscription_id(), new_subscription_id()
    assert a != b
    assert len(a) == 32 and a.isalnum()  # uuid4 hex


def test_ensure_subscription_id_is_idempotent():
    s = Subscriber(mobile="9199", plan="monthly")
    first = s.ensure_subscription_id()
    second = s.ensure_subscription_id()
    assert first == second and first


def test_subscriber_roundtrip_includes_subscription_id():
    s = Subscriber(mobile="9199", plan="monthly", subscription_id="abc123")
    assert Subscriber.from_row(s.to_row()).subscription_id == "abc123"


def test_repo_find_by_subscription_id(repos):
    s = Subscriber(mobile="9199", plan="monthly", status=SubscriberStatus.ACTIVE,
                   subscription_id="tok-xyz")
    repos["subscribers"].append(s)
    found = repos["subscribers"].find_by_subscription_id("tok-xyz")
    assert found is not None and found.mobile == "9199"
    assert repos["subscribers"].find_by_subscription_id("nope") is None


def test_upsert_pending_assigns_subscription_id(repos, plans):
    svc = SubscriberService(repos["subscribers"], repos["payments"], plans)
    sub = svc.upsert_pending("9199", "monthly")
    assert sub.subscription_id  # generated at signup
    assert repos["subscribers"].find("9199").subscription_id == sub.subscription_id


def test_subscriber_name_roundtrip():
    s = Subscriber(mobile="9199", plan="monthly", name="Ravi Kumar")
    assert Subscriber.from_row(s.to_row()).name == "Ravi Kumar"


def test_upsert_pending_stores_name(repos, plans):
    svc = SubscriberService(repos["subscribers"], repos["payments"], plans)
    svc.upsert_pending("9199", "monthly", "Ravi")
    assert repos["subscribers"].find("9199").name == "Ravi"


def test_upsert_pending_blank_name_does_not_overwrite(repos, plans):
    svc = SubscriberService(repos["subscribers"], repos["payments"], plans)
    svc.upsert_pending("9199", "monthly", "Ravi")
    svc.upsert_pending("9199", "monthly", "")   # e.g. later message with no profile name
    assert repos["subscribers"].find("9199").name == "Ravi"  # preserved


def test_template_uses_name_for_var1(repos, plans):
    repos["subscribers"].append(Subscriber(
        mobile="9199", plan="monthly", status=SubscriberStatus.ACTIVE,
        start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), opt_in=True,
        subscription_id="tok-9199", name="Ravi",
    ))
    repos["payments"].append(Payment(reference_id="DD2601019199", mobile="9199",
                                     plan="monthly", amount=49, status=PaymentStatus.SUCCESS))
    wa = FakeWhatsApp()
    _template_delivery(repos, wa, plans).deliver(date(2026, 8, 19))
    params = wa.sent[0]["params"]
    assert params[0] == "Ravi"                       # {{1}} = real name
    assert params[1].endswith("/tok-9199")           # {{2}} = per-sub URL


def test_template_var1_falls_back_when_no_name(repos, plans):
    repos["subscribers"].append(Subscriber(
        mobile="9199", plan="monthly", status=SubscriberStatus.ACTIVE,
        start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), opt_in=True,
        subscription_id="tok-9199", name="",
    ))
    repos["payments"].append(Payment(reference_id="DD2601019199", mobile="9199",
                                     plan="monthly", amount=49, status=PaymentStatus.SUCCESS))
    wa = FakeWhatsApp()
    _template_delivery(repos, wa, plans).deliver(date(2026, 8, 19))
    # No PII: falls back to a generic greeting, not the mobile number.
    assert wa.sent[0]["params"][0] == "devotee"


# ------------------------- #1 adapter payload ------------------------- #

def test_meta_send_template_params_payload(monkeypatch):
    from adapters.whatsapp import MetaWhatsAppClient
    captured = {}

    client = MetaWhatsAppClient(access_token="t", phone_number_id="pid")

    def fake_post(payload):
        captured.update(payload)
        from application.ports.whatsapp import WhatsAppResult
        return WhatsAppResult(ok=True, message_id="m1")

    monkeypatch.setattr(client, "_post", fake_post)
    client.send_template_params("9199", "daily_darshan_status",
                                ["9199", "https://d.example/tok"], "en")

    assert captured["type"] == "template"
    tmpl = captured["template"]
    assert tmpl["name"] == "daily_darshan_status"
    assert tmpl["language"]["code"] == "en"
    body = tmpl["components"][0]
    assert body["type"] == "body"
    assert [p["text"] for p in body["parameters"]] == ["9199", "https://d.example/tok"]


# ------------------------- #2 template delivery mode ------------------------- #

def _seed_active(repos, mobile="9199", subid="tok-9199"):
    repos["subscribers"].append(Subscriber(
        mobile=mobile, plan="monthly", status=SubscriberStatus.ACTIVE,
        start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), opt_in=True,
        subscription_id=subid,
    ))
    repos["payments"].append(Payment(
        reference_id=f"DD260101{mobile[-4:]}", mobile=mobile, plan="monthly",
        amount=49, status=PaymentStatus.SUCCESS,
    ))


def _template_delivery(repos, wa, plans):
    elig = SubscriberService(repos["subscribers"], repos["payments"], plans, repos["sentlog"])
    return DeliveryService(
        repos["subscribers"], repos["sentlog"], wa, elig,
        delivery_mode="utility_template", template_name="daily_darshan_status",
        template_lang="en", page_base_url="https://darshan.example.com",
        max_retries=1,
    )


def test_template_mode_sends_per_subscriber_url(repos, plans):
    _seed_active(repos, "9199", "tok-9199")
    wa = FakeWhatsApp()
    report = _template_delivery(repos, wa, plans).deliver(date(2026, 8, 19))
    assert report.sent == 1
    call = wa.sent[0]
    assert call["type"] == "template_params"
    assert call["template"] == "daily_darshan_status"
    # {{2}} is the per-subscriber page URL built from subscription_id.
    assert call["params"][1] == "https://darshan.example.com/tok-9199"


def test_template_mode_idempotent(repos, plans):
    _seed_active(repos, "9199", "tok-9199")
    wa = FakeWhatsApp()
    svc = _template_delivery(repos, wa, plans)
    svc.deliver(date(2026, 8, 19))
    report2 = svc.deliver(date(2026, 8, 19))
    assert report2.sent == 0 and report2.skipped == 1


def test_template_mode_skips_subscriber_without_subid(repos, plans):
    # Active + paid but no subscription_id -> cannot build URL -> skipped.
    repos["subscribers"].append(Subscriber(
        mobile="9200", plan="monthly", status=SubscriberStatus.ACTIVE,
        start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), opt_in=True,
        subscription_id="",
    ))
    repos["payments"].append(Payment(reference_id="DD2601019200", mobile="9200",
                                     plan="monthly", amount=49, status=PaymentStatus.SUCCESS))
    wa = FakeWhatsApp()
    report = _template_delivery(repos, wa, plans).deliver(date(2026, 8, 19))
    assert report.sent == 0 and report.skipped == 1
    assert wa.sent == []


def test_template_mode_aborts_without_config(repos, plans):
    _seed_active(repos)
    elig = SubscriberService(repos["subscribers"], repos["payments"], plans, repos["sentlog"])
    svc = DeliveryService(repos["subscribers"], repos["sentlog"], FakeWhatsApp(), elig,
                          delivery_mode="utility_template", template_name="",
                          page_base_url="")
    report = svc.deliver(date(2026, 8, 19))
    assert report.sent == 0


# ------------------------- #3 page renderer ------------------------- #

def test_page_renderer_writes_per_subscription_file(tmp_path):
    r = PageRenderer(pages_dir="docs", image_public_base="https://u.github.io/dd")
    sub = Subscriber(mobile="9199", plan="monthly", end_date=date(2026, 12, 31),
                     subscription_id="tok-9199")
    rel = r.write_page(sub, date(2026, 8, 19), delivered=True, root=str(tmp_path))
    assert rel == "docs/tok-9199/index.html"
    html_text = (tmp_path / rel).read_text()
    assert "2026-08-19" in html_text
    assert "Delivered" in html_text
    assert "https://u.github.io/dd/images/2026-08-19.jpg" in html_text
    assert 'property="og:image"' in html_text
    assert "noindex" in html_text
    # No PII: the mobile number must not appear on the public page.
    assert "9199" not in html_text


def test_page_renderer_skips_subscriber_without_subid(tmp_path):
    r = PageRenderer(image_public_base="https://u.github.io/dd")
    sub = Subscriber(mobile="9199", plan="monthly", subscription_id="")
    assert r.write_page(sub, date(2026, 8, 19), root=str(tmp_path)) is None


def test_page_renderer_write_all_counts_only_valid(tmp_path):
    r = PageRenderer(image_public_base="https://u.github.io/dd")
    subs = [
        Subscriber(mobile="1", plan="monthly", subscription_id="a"),
        Subscriber(mobile="2", plan="monthly", subscription_id=""),   # skipped
        Subscriber(mobile="3", plan="monthly", subscription_id="c"),
    ]
    written = r.write_all(subs, date(2026, 8, 19), root=str(tmp_path))
    assert len(written) == 2


# ------------------------- migration ------------------------- #

def test_backfill_assigns_ids_to_idless_rows(repos, monkeypatch):
    # Two rows without subscription_id, one with.
    repos["subscribers"].append(Subscriber(mobile="1", plan="monthly", subscription_id=""))
    repos["subscribers"].append(Subscriber(mobile="2", plan="monthly", subscription_id="keep"))

    from migrations.backfill_subscription_ids import backfill

    class FakeContainer:
        subscribers = repos["subscribers"]
        class logs:
            @staticmethod
            def log(*a, **k): pass

    count = backfill(FakeContainer)
    assert count == 1  # only the id-less row
    assert repos["subscribers"].find("1").subscription_id  # now assigned
    assert repos["subscribers"].find("2").subscription_id == "keep"  # untouched
