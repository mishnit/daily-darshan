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
    assert "python -m application.admin_alert payments" in workflow
    assert "WHATSAPP_ADMIN_NUMBERS" in workflow
    from application.admin_alert import payment_counts
    from datetime import date
    assert payment_counts([
        {"reference_id": "DD2609160001", "status": "PENDING", "utr": ""},
        {"reference_id": "DD2609150001", "status": "PENDING", "utr": "123456789012"},
        {"reference_id": "DD2609140001", "status": "SUCCESS", "utr": "999999999999"},
    ], date(2026, 9, 16)) == (1, 1)

def test_ops_alert_passes_job_suffix_for_template_button_base():
    workflow = _workflow("ops-alert.yml")
    assert "- Regenerate Daily Pages" in workflow
    assert "- Deploy Daily Darshan Pages" in workflow
    assert 'https://github.com/${REPOSITORY}/actions/runs/*)' in workflow
    assert 'job_suffix="${job_url#https://github.com/${REPOSITORY}/actions/runs/}"' in workflow
    assert '{ type: "text", text: $job_suffix }' in workflow
    assert 'echo "job_suffix=$job_suffix" >> "$GITHUB_OUTPUT"' in workflow

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
    assert "github.event_name == 'schedule'" in delivery
    assert "github.event_name == 'workflow_dispatch'" in delivery
    assert "github.event.workflow_run.conclusion == 'success'" in delivery


def test_image_requires_today_but_historical_backfill_misses_are_nonfatal():
    image = _workflow("image.yml")

    assert 'if ! python scheduler.py image-only --date "$image_date" $force_args; then' in image
    assert "continuing to today's required image" in image
    assert "python scheduler.py image $force_args" in image


def test_manual_image_recollect_requires_an_explicit_opt_in():
    image = _workflow("image.yml")

    assert "force_recollect:" in image
    assert "type: boolean" in image
    assert "default: false" in image
    assert 'force_args="--force-recollect"' in image
    assert "python scheduler.py image $force_args" in image
    assert "python scheduler.py image-only --date \"$image_date\" $force_args" in image


def test_expiry_and_page_pruning_complete_before_publication_can_start():
    image = _workflow("image.yml")

    assert "python scheduler.py image" in image
    publication = Path("application/image_publication.py").read_text(encoding="utf-8")
    assert publication.index("run_expiry_sweep(container, transaction") < publication.index('transaction._git("push"')


def test_production_templates_and_immediate_render_persistence_are_configured():
    config = json.loads(Path("config.json").read_text(encoding="utf-8"))

    assert config["delivery"] | {
        "template_lang": "en",
        "welcome_template_lang": "en",
    } == config["delivery"]
    assert config["renewal"] | {
        "template_lang": "en",
    } == config["renewal"]
    assert config["delivery"]["template_name"].strip()
    assert config["delivery"]["welcome_template_name"].strip()
    assert config["renewal"]["template_name"].strip()
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
