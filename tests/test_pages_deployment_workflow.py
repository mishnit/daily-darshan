from pathlib import Path


def test_pages_deploys_once_after_image_workflow_or_manual_dispatch():
    workflow = Path(".github/workflows/deploy-pages.yml").read_text(encoding="utf-8")

    assert 'workflows: ["Daily Image", "Regenerate Daily Pages"]' in workflow
    assert "types: [completed]" in workflow
    assert "schedule:" not in workflow
    assert "workflow_dispatch: {}" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.head_branch == github.event.repository.default_branch" in workflow
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

    publication = Path("application/image_publication.py").read_text(encoding="utf-8")
    assert "not regenerate_only or on_date == today_ist()" in publication
    assert publication.index("run_expiry_sweep(container, transaction") < publication.index('transaction._git("push"')
    assert "ref: main" in workflow
    assert 'if ! python scheduler.py image-only --date "$image_date" $force_args; then' in workflow
    assert "continuing to today's required image" in workflow
    assert "python scheduler.py image-only $force_args" in workflow
    assert "needs: [image, approval]" in workflow
    assert "run: python scheduler.py pages" in workflow


def test_daily_image_deploy_requires_its_dedicated_pages_job():
    deploy = Path(".github/workflows/deploy-pages.yml").read_text(encoding="utf-8")

    assert 'github.event.workflow_run.name == "Daily Image"' not in deploy
    assert 'select(.name == "pages") | .conclusion' in deploy


def test_whatsapp_templates_match_documented_meta_configuration():
    import json

    config = json.loads(Path("config.json").read_text(encoding="utf-8"))

    assert config["delivery"]["template_name"].strip()
    assert config["delivery"]["template_lang"] == "en"
    assert config["delivery"]["welcome_template_name"].strip()
    assert config["delivery"]["welcome_template_lang"] == "en"
    assert config["renewal"]["template_name"].strip()
    assert config["renewal"]["template_lang"] == "en"
    assert "quiet_window_utc" not in config["persistence"]
