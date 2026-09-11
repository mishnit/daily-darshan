from pathlib import Path


def test_renewal_and_delivery_are_serialized_with_a_gap():
    workflow = Path(".github/workflows/delivery.yml").read_text(encoding="utf-8")

    renewal = workflow.index("- name: Send renewal reminders")
    gap = workflow.index("- name: Keep renewal and delivery messages apart")
    delivery = workflow.index("- name: Deliver today's image")

    assert renewal < gap < delivery
    assert "group: daily-darshan-repository-writes" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "WHATSAPP_MESSAGE_GAP_SECONDS" in workflow
    assert "id: renewal" in workflow
    assert "RENEWAL_SENT_COUNT: ${{ steps.renewal.outputs.sent_count || '0' }}" in workflow
    assert 'if [ "$renewal_sent" -eq 0 ]; then' in workflow
    assert "No renewal reminder was sent; proceeding immediately" in workflow
    assert 'echo "deliver=false" >> "$GITHUB_OUTPUT"' not in workflow
    assert "Renewal recipients are protected by the daily send ledger" in workflow
    assert "if: steps.message_gap.outputs.deliver == 'true'" in workflow
    assert 'sleep "$gap_seconds"' in workflow


def test_delivery_runs_after_successful_pages_deployment_or_manual_dispatch():
    workflow = Path(".github/workflows/delivery.yml").read_text(encoding="utf-8")

    assert "schedule:" not in workflow
    assert 'workflows: ["Deploy Daily Darshan Pages"]' in workflow
    assert "types: [completed]" in workflow
    assert "workflow_dispatch: {}" in workflow
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
