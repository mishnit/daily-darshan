import pytest
from adapters.repo_sync import RepoSync


class Remote:
    snapshot_unchanged = False

    def begin_snapshot(self):
        pass

    def read_files(self, paths):
        if self.fail:
            raise RuntimeError('download failed')
        return {p: self.content for p in paths}


def test_failed_refresh_cannot_reuse_old_baseline(tmp_path):
    remote = Remote()
    remote.fail, remote.content = False, b'old\n'
    sync = RepoSync(remote, str(tmp_path), ['state.csv'], True)
    sync.pull(strict=True)
    remote.fail, remote.content = True, b'new\n'
    with pytest.raises(RuntimeError):
        sync.pull(strict=True)
    remote.fail, remote.snapshot_unchanged = False, True
    sync.pull(strict=True)
    assert (tmp_path / 'state.csv').read_bytes() == b'new\n'


def test_unchanged_snapshot_restores_uncommitted_local_edits(tmp_path):
    remote = Remote()
    remote.fail, remote.content = False, b'committed\n'
    sync = RepoSync(remote, str(tmp_path), ['state.csv'], True)
    sync.pull(strict=True)
    (tmp_path / 'state.csv').write_bytes(b'uncommitted\n')
    remote.snapshot_unchanged = True
    sync.pull(strict=True)
    assert (tmp_path / 'state.csv').read_bytes() == b'committed\n'
