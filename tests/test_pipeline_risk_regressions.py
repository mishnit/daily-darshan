"""Regression contracts for the image → Pages → WhatsApp production pipeline."""

from __future__ import annotations

import json
from pathlib import Path


WORKFLOWS = Path(".github/workflows")


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_automatic_chain_publishes_before_whatsapp_delivery():
    image = _workflow("image.yml")
    deploy = _workflow("deploy-pages.yml")
    delivery = _workflow("delivery.yml")

    assert 'workflows: ["Daily Image", "Regenerate Daily Pages"]' in deploy
    assert 'workflows: ["Deploy Daily Darshan Pages"]' in delivery
    assert "actions/deploy-pages@v4" not in image
    assert "actions/deploy-pages@v4" not in delivery
    assert deploy.count("actions/deploy-pages@v4") == 1


def test_render_and_ordinary_main_pushes_cannot_deploy_pages():
    deploy = _workflow("deploy-pages.yml")

    assert "\n  push:" not in deploy
    assert "\n  schedule:" not in deploy
    assert "workflow_dispatch: {}" in deploy


def test_failed_or_non_default_image_run_fails_publication_gate():
    deploy = _workflow("deploy-pages.yml")

    assert "github.event.workflow_run.conclusion == 'success'" in deploy
    assert "github.event.workflow_run.head_branch == github.event.repository.default_branch" in deploy
    assert deploy.index("if: >-") < deploy.index("runs-on:")


def test_delivery_can_run_manually_but_has_no_independent_schedule():
    delivery = _workflow("delivery.yml")

    assert "workflow_dispatch: {}" in delivery
    assert "\n  schedule:" not in delivery
    assert "github.event_name == 'workflow_dispatch'" in delivery
    assert "github.event.workflow_run.conclusion == 'success'" in delivery


def test_image_requires_today_but_historical_backfill_misses_are_nonfatal():
    image = _workflow("image.yml")

    assert 'if ! python scheduler.py image-only --date "$image_date"; then' in image
    assert "continuing to today's required image" in image
    assert "python scheduler.py image\n" in image


def test_expiry_and_page_pruning_complete_before_publication_can_start():
    image = _workflow("image.yml")

    fetch = image.index("- name: Fetch and store daily images")
    expiry = image.index(
        "- name: Expire subscriptions and prune inactive pages before publication"
    )
    assert fetch < expiry
    assert "run: python scheduler.py expiry" in image[expiry:]


def test_production_templates_and_immediate_render_persistence_are_configured():
    config = json.loads(Path("config.json").read_text(encoding="utf-8"))

    assert config["delivery"] | {
        "template_name": "daily_darshan_delivery_update",
        "template_lang": "en",
    } == config["delivery"]
    assert config["renewal"] | {
        "template_name": "daily_darshan_delivery_update",
        "template_lang": "en",
    } == config["renewal"]
    assert config["persistence"]["quiet_window_utc"] == {"start": "", "end": ""}


def test_operator_docs_do_not_restore_obsolete_crons_or_legacy_pages_mode():
    docs = "\n".join(
        Path(name).read_text(encoding="utf-8")
        for name in ("README.md", "DEPLOYMENT.md", "sequence-diagrams.md")
    )

    for obsolete in ("04:49", "05:04", "10:19", "10:34", "[3,1]"):
        assert obsolete not in docs
    assert "*Deploy from a branch*" not in docs
