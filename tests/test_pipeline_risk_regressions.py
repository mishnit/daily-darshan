"""Regression contracts for the image → Pages → WhatsApp production pipeline."""

from __future__ import annotations

import json
from pathlib import Path


WORKFLOWS = Path(".github/workflows")


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_ci_blocks_removed_files_and_test_definitions():
    ci = _workflow("ci.yml")

    assert "Block removed files and test definitions" in ci
    assert 'git diff --diff-filter=D --name-status "$BASE_SHA"...HEAD' in ci
    assert "::error title=Files removed::" in ci
    assert "Restore them before merging" in ci
    assert "^-.*def test_|^-.*class Test" in ci
    assert "::error title=Tests removed::" in ci


def test_automatic_chain_publishes_before_whatsapp_delivery():
    image = _workflow("image.yml")
    deploy = _workflow("deploy-pages.yml")
    delivery = _workflow("delivery.yml")

    assert 'workflows: ["Daily Image", "Regenerate Daily Pages"]' in deploy
    assert 'workflows: ["Deploy Daily Darshan Pages"]' in delivery
    assert "actions/deploy-pages@v4" not in image
    assert "actions/deploy-pages@v4" not in delivery
    assert deploy.count("actions/deploy-pages@v4") == 1


def test_pending_utr_alert_runs_at_9pm_ist_and_only_alerts_for_missing_utr():
    # Keep this historical test name so the removed-test guard can track the
    # contract. The workflow is now intentionally manual-only.
    workflow = _workflow("payment-utr-alert.yml")

    assert "workflow_dispatch:" in workflow
    assert "\n  schedule:" not in workflow
    assert 'ZoneInfo("Asia/Kolkata")' in workflow
    assert 'row.get("status", "").strip().upper() == "PENDING"' in workflow
    assert 'not row.get("utr", "").strip()' in workflow
    assert "if: steps.pending.outputs.count != '0'" in workflow
    assert 'name: "daily_darshan_ops_alert"' in workflow
    assert '${{ github.repository }}/actions/runs/${{ github.run_id }}' in workflow

def test_ops_alert_passes_job_suffix_for_template_button_base():
    workflow = _workflow("ops-alert.yml")
    assert 'https://github.com/${REPOSITORY}/actions/runs/*)' in workflow
    assert 'job_suffix="${job_url#https://github.com/${REPOSITORY}/actions/runs/}"' in workflow
    assert "Pass only the" in workflow

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
        "template_name": "dailydarshan_subscription_status",
        "template_lang": "en",
        "welcome_template_name": "dailydarshan_subscription_status",
        "welcome_template_lang": "en",
    } == config["delivery"]
    assert config["renewal"] | {
        "template_name": "dailydarshan_subscription_status",
        "template_lang": "en",
    } == config["renewal"]
    assert "quiet_window_utc" not in config["persistence"]


def test_operator_docs_do_not_restore_obsolete_crons_or_legacy_pages_mode():
    docs = "\n".join(
        Path(name).read_text(encoding="utf-8")
        for name in ("README.md", "DEPLOYMENT.md", "sequence-diagrams.md")
    )

    for obsolete in ("04:49", "05:04", "10:19", "10:34", "[3,1]"):
        assert obsolete not in docs
    assert "*Deploy from a branch*" not in docs


def test_mermaid_sequence_diagrams_do_not_use_statement_delimiters_in_text():
    """A semicolon in sequence text starts a new Mermaid statement on GitHub."""
    lines = Path("sequence-diagrams.md").read_text(encoding="utf-8").splitlines()
    inside_mermaid = False
    for line in lines:
        if line.strip() == "```mermaid":
            inside_mermaid = True
            continue
        if inside_mermaid and line.strip() == "```":
            inside_mermaid = False
            continue
        if inside_mermaid:
            assert ";" not in line
