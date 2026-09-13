from pathlib import Path


def test_renewal_and_delivery_are_serialized_without_blocking_runner_sleep():
    workflow = Path(".github/workflows/delivery.yml").read_text(encoding="utf-8")

    renewal = workflow.index("- name: Send renewal reminders")
    delivery = workflow.index("- name: Deliver today's image")

    assert renewal < delivery
    assert "group: daily-darshan-repository-writes" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "id: renewal" in workflow
    assert "WHATSAPP_MESSAGE_GAP_SECONDS" not in workflow
    assert "sleep " not in workflow


def test_delivery_runs_after_successful_pages_deployment_or_manual_dispatch():
    workflow = Path(".github/workflows/delivery.yml").read_text(encoding="utf-8")

    assert "schedule:" not in workflow
    assert 'workflows: ["Deploy Daily Darshan Pages"]' in workflow
    assert "types: [completed]" in workflow
    assert "workflow_dispatch: {}" in workflow
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
