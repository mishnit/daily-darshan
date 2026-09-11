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
    assert 'echo "deliver=false" >> "$GITHUB_OUTPUT"' in workflow
    assert "if: steps.message_gap.outputs.deliver == 'true'" in workflow
    assert 'sleep "$gap_seconds"' in workflow
