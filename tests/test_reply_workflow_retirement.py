from pathlib import Path


def test_reply_retry_workflow_is_retired():
    assert not Path('.github/workflows/retry-replies.yml').exists()
    assert all('internal/retry-replies' not in path.read_text()
               for path in Path('.github/workflows').glob('*.yml'))


def test_deleted_file_guard_exception_is_scoped_to_retired_workflow():
    import subprocess
    command = "grep -v $'^D\\t.github/workflows/retry-replies.yml$' || true"
    result = subprocess.run(['bash', '-c', command], input=(
        'D\t.github/workflows/retry-replies.yml\nD\tmain.py\nD\ttests/test_example.py\n'),
        text=True, capture_output=True, check=True)
    assert result.stdout.splitlines() == ['D\tmain.py', 'D\ttests/test_example.py']
