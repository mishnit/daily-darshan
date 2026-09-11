from pathlib import Path


def test_pages_deploys_once_after_successful_delivery_workflow():
    workflow = Path(".github/workflows/deploy-pages.yml").read_text(encoding="utf-8")

    assert 'workflows: ["Daily Delivery"]' in workflow
    assert "types: [completed]" in workflow
    assert "schedule:" not in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
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
