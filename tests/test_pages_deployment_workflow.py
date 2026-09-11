from pathlib import Path


def test_pages_deploys_once_after_image_workflow_or_manual_dispatch():
    workflow = Path(".github/workflows/deploy-pages.yml").read_text(encoding="utf-8")

    assert 'workflows: ["Daily Image"]' in workflow
    assert "types: [completed]" in workflow
    assert "schedule:" not in workflow
    assert "workflow_dispatch: {}" in workflow
    assert 'test "$IMAGE_CONCLUSION" = "success"' in workflow
    assert 'test "$IMAGE_BRANCH" = "$DEFAULT_BRANCH"' in workflow
    assert "group: pages" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "actions/upload-pages-artifact@v4" in workflow
    assert "path: docs" in workflow
    assert "actions/deploy-pages@v4" in workflow


def test_image_and_delivery_workflows_do_not_deploy_pages_directly():
    for path in (
        ".github/workflows/image.yml",
        ".github/workflows/delivery.yml",
    ):
        workflow = Path(path).read_text(encoding="utf-8")
        assert "actions/deploy-pages" not in workflow
        assert "actions/upload-pages-artifact" not in workflow


def test_image_expires_and_prunes_pages_before_triggering_publication():
    workflow = Path(".github/workflows/image.yml").read_text(encoding="utf-8")

    assert "Expire subscriptions and prune inactive pages before publication" in workflow
    assert "run: python scheduler.py expiry" in workflow
    assert workflow.index("python scheduler.py image") < workflow.index(
        "Expire subscriptions and prune inactive pages before publication"
    )
    assert 'if ! python scheduler.py image-only --date "$image_date"; then' in workflow
    assert "continuing to today's required image" in workflow


def test_whatsapp_templates_match_documented_meta_configuration():
    import json

    config = json.loads(Path("config.json").read_text(encoding="utf-8"))

    assert config["delivery"]["template_name"] == "daily_darshan_delivery_update"
    assert config["delivery"]["template_lang"] == "en_US"
    assert config["renewal"]["template_name"] == "daily_darshan_renewal"
    assert config["renewal"]["template_lang"] == "en_US"
    assert config["persistence"]["quiet_window_utc"] == {"start": "", "end": ""}
