"""Job directories in /tmp: removed after the job, and swept only once no living process holds them."""

import os
import signal
import subprocess  # nosec B404
import sys
import time
from pathlib import Path

import pytest

from core import job_directories
from core.job_directories import (
    LOCK_NAME,
    PREFIX,
    STAGING_PREFIX,
    job_directory,
    remove_abandoned_job_directories,
)

APP = Path(__file__).resolve().parent.parent


def test_a_job_directory_is_removed_when_the_job_ends(tmp_path: Path) -> None:
    with job_directory("photo-album-video", root=tmp_path) as directory:
        assert directory.name.startswith(f"{PREFIX}photo-album-video-")
        (directory / "source.mov").write_bytes(b"video")
    assert list(tmp_path.iterdir()) == []


def test_a_job_directory_is_removed_when_the_job_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), job_directory("photo-album-video", root=tmp_path) as directory:
        (directory / "source.mov").write_bytes(b"video")
        raise RuntimeError
    assert list(tmp_path.iterdir()) == []


def test_the_sweep_leaves_a_directory_a_running_job_holds(tmp_path: Path) -> None:
    """flock locks of two opens of one file exclude each other, even in one process: so the sweep in
    this test meets the lock exactly as a sweep in another process would."""
    with job_directory("photo-album-video", root=tmp_path) as directory:
        (directory / "source.mov").write_bytes(b"video")
        os.utime(directory, (0, 0))  # however old, a held directory is in use

        assert remove_abandoned_job_directories(tmp_path).directories == 0
        assert (directory / "source.mov").exists()


def test_the_sweep_removes_what_a_killed_job_left(tmp_path: Path) -> None:
    """The case it exists for: a job SIGKILLed (OOM killer, hard time limit) mid-encode."""
    ready = tmp_path / "ready"
    (tmp_path / "jobs").mkdir()
    job = subprocess.Popen(  # nosec B603
        [
            sys.executable,
            "-c",
            (
                "import sys, time; from pathlib import Path\n"
                "from core.job_directories import job_directory\n"
                "with job_directory('photo-album-video', root=Path(sys.argv[1])) as d:\n"
                "    (d / 'source.mov').write_bytes(b'x' * 1000)\n"
                "    Path(sys.argv[2]).touch()\n"
                "    time.sleep(60)\n"
            ),
            str(tmp_path / "jobs"),
            str(ready),
        ],
        cwd=APP,
    )
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert job.poll() is None, "the job ended early"
            assert time.monotonic() < deadline, "the job never started"
            time.sleep(0.05)
        assert remove_abandoned_job_directories(tmp_path / "jobs").directories == 0  # still running
    finally:
        job.send_signal(signal.SIGKILL)
        job.wait()
    (left,) = (tmp_path / "jobs").iterdir()  # the kill skipped the job's own cleanup

    removed = remove_abandoned_job_directories(tmp_path / "jobs")

    assert (removed.directories, removed.bytes) == (1, 1000)
    assert not left.exists()


def test_the_sweep_removes_an_unheld_directory_however_new(tmp_path: Path) -> None:
    """Its lock is free only once its owner is gone, which is all the sweep needs to know."""
    directory = tmp_path / f"{PREFIX}gahkflix-abc"
    directory.mkdir()
    (directory / LOCK_NAME).touch()

    assert remove_abandoned_job_directories(tmp_path).directories == 1
    assert not directory.exists()


@pytest.mark.parametrize("name", [f"{STAGING_PREFIX}abc", f"{PREFIX}photo-album-import-abc"])
def test_a_directory_without_a_lock_is_removed_only_once_it_is_old(tmp_path: Path, name: str) -> None:
    """A staging directory (killed between mkdtemp and rename), or a job directory whose lock file
    its owner's own rmtree has already taken — and which that owner may still be removing."""
    directory = tmp_path / name
    directory.mkdir()

    assert remove_abandoned_job_directories(tmp_path).directories == 0

    old = time.time() - job_directories.UNLOCKED_ABANDONED_AFTER - 1
    os.utime(directory, (old, old))
    assert remove_abandoned_job_directories(tmp_path).directories == 1
    assert not directory.exists()


def test_the_sweep_leaves_everything_else_in_tmp(tmp_path: Path) -> None:
    for name in ("heartbeat-media", "photo-album-abc", "pymp-abc"):
        (tmp_path / name).mkdir()
    (tmp_path / f"{PREFIX}a-file").touch()

    assert remove_abandoned_job_directories(tmp_path).directories == 0
    assert len(list(tmp_path.iterdir())) == 4


def test_a_new_worker_process_sweeps(monkeypatch: pytest.MonkeyPatch) -> None:
    """worker_process_init: how a pool process lost to a kill is replaced, so the sweep runs then."""
    from celery.signals import worker_process_init

    swept: list[bool] = []
    monkeypatch.setattr(job_directories, "remove_abandoned_job_directories", lambda: swept.append(True))
    worker_process_init.send(sender=None)

    assert swept == [True]
