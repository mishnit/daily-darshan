"""Safety contracts for a manual historical Pages refresh."""
from __future__ import annotations

import hashlib
import json
from datetime import date

from application.image_approval import approval_ready, deployment_ready


def test_historical_approval_and_deployment_validate_the_same_selected_date(tmp_path):
    selected_date = date(2026, 9, 21)
    config = {
        "admin": {"require_image_approval": True},
        "paths": {"image_reviews_csv": "csv/image_reviews.csv", "images_dir": "docs/images"},
        "delivery": {"pages_dir": "docs"},
    }
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "csv").mkdir()
    payload = b"approved-image"
    digest = hashlib.sha256(payload).hexdigest()
    (tmp_path / "csv" / "image_reviews.csv").write_text(
        "id,date,status,sha256\nselected,2026-09-21,APPROVED," + digest + "\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "images").mkdir(parents=True)
    (tmp_path / "docs" / "images" / "selected_2026-09-21.jpg").write_bytes(payload)
    (tmp_path / "docs" / "image-approval.json").write_text(
        json.dumps({"id": "selected", "date": "2026-09-21", "sha256": digest}),
        encoding="utf-8",
    )

    assert approval_ready(tmp_path, selected_date) is True
    assert deployment_ready(tmp_path, selected_date) is True
    assert approval_ready(tmp_path, date(2026, 9, 22)) is False
    assert deployment_ready(tmp_path, date(2026, 9, 22)) is False


def test_pages_workflow_has_a_safe_today_or_yesterday_choice():
    workflow = open(".github/workflows/pages.yml", encoding="utf-8").read()

    assert "options: [today, yesterday]" in workflow
    assert "application.image_approval --date" in workflow
    assert 'scheduler.py pages --date "$RENDER_DATE"' in workflow


def test_pages_deploy_uses_the_date_stamped_in_the_rendered_artifact():
    workflow = open(".github/workflows/deploy-pages.yml", encoding="utf-8").read()

    assert 'json.load(open("docs/image-approval.json"))["date"]' in workflow
    assert "application.image_approval --published --date" in workflow
    assert "actions: read" in workflow
    assert 'select(.name == "pages") | .conclusion' in workflow
