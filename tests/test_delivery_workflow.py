from pathlib import Path


def test_renewal_and_delivery_are_serialized_with_a_gap():
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

    assert 'workflows: ["Deploy Daily Darshan Pages"]' in workflow
    assert "types: [completed]" in workflow
    assert "workflow_dispatch: {}" in workflow
    assert "github.event_name == 'schedule'" in workflow
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow


def test_each_contact_phase_refreshes_main_before_reading_business_csvs():
    workflow = Path(".github/workflows/delivery.yml").read_text(encoding="utf-8")

    phases = [
        ("Refresh repository before welcome", "Send committed activation welcomes"),
        ("Refresh repository before renewal", "Send renewal reminders"),
        ("Refresh repository before delivery", "Deliver today's image"),
    ]
    for refresh, phase in phases:
        assert workflow.index(refresh) < workflow.index(phase)
    assert workflow.count('git fetch origin "${{ github.event.repository.default_branch }}"') == 3
    assert workflow.count('git rebase "origin/${{ github.event.repository.default_branch }}"') == 3
