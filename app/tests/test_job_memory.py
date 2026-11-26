"""A worker job's peak memory: its own process's and its largest program's, kept on its record."""

import os
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core import job_memory, job_progress, worker_logs
from core.job_progress import FLUSH_INTERVAL, tick
from core.models import WorkerJobLog
from residents.models import Resident, Role

MB = 1 << 20


def _process(proc: Path, pid: int, parent: int, name: str, peak_kb: int) -> None:
    directory = proc / str(pid)
    directory.mkdir()
    (directory / "stat").write_text(f"{pid} ({name}) S {parent} {pid} {pid} 0 -1 4194560")
    (directory / "comm").write_text(f"{name}\n")
    (directory / "status").write_text(
        f"Name:\t{name}\nVmPeak:\t 9999999 kB\nVmHWM:\t {peak_kb} kB\nVmRSS:\t 1 kB\n"
    )


@pytest.fixture
def proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A /proc of our own: this process, its programs, and one that is not its."""
    monkeypatch.setattr(job_memory, "PROC", tmp_path)
    me = os.getpid()
    _process(tmp_path, me, 1, "celery", 400 * 1024)
    _process(tmp_path, 90001, me, "sh", 2 * 1024)
    _process(tmp_path, 90002, 90001, "ffmpeg", 900 * 1024)  # a grandchild, as under `sh -c`
    _process(tmp_path, 90003, me, "exiv2 (x) y", 30 * 1024)  # a name with brackets and a space
    _process(tmp_path, 90004, 1, "postgres", 5000 * 1024)  # not this process's
    return tmp_path


def test_the_largest_program_is_found_below_the_process_however_deep(proc: Path) -> None:
    assert job_memory.largest_program() == ("ffmpeg", 900 * MB)


def test_the_process_own_peak_and_its_reset(proc: Path) -> None:
    assert job_memory.own_peak() == 400 * MB

    job_memory.reset()

    # The kernel's own instruction for "start the high-water mark again" (proc(5), clear_refs).
    assert (proc / str(os.getpid()) / "clear_refs").read_text() == "5"


def test_nothing_is_measured_without_proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job_memory, "PROC", tmp_path / "absent")
    job_memory.reset()
    assert (job_memory.own_peak(), job_memory.largest_program()) == (None, None)


def test_a_job_keeps_its_largest_readings(monkeypatch: pytest.MonkeyPatch) -> None:
    job = job_progress.LiveJob("job", timezone.now(), [])
    job.note_memory(300 * MB, ("ffmpeg", 900 * MB))
    job.note_memory(350 * MB, ("ffmpeg", 200 * MB))  # the next encode, smaller
    job.note_memory(None, None)  # the programs have ended

    assert (job.peak_memory, job.largest_program) == (350 * MB, ("ffmpeg", 900 * MB))


@pytest.mark.django_db
def test_the_peak_is_written_while_the_job_runs_so_a_killed_job_keeps_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On 2026-11-15 a job took the worker to 1.4 GB and was killed for it; nothing said which."""
    readings = {"own": 300 * MB, "program": ("ffmpeg", 900 * MB)}
    monkeypatch.setattr(job_memory, "reset", lambda: None)
    monkeypatch.setattr(job_memory, "own_peak", lambda: readings["own"])
    monkeypatch.setattr(job_memory, "largest_program", lambda: readings["program"])
    task = SimpleNamespace(name="gahkflix.tasks.process_video_file", request=SimpleNamespace(is_eager=True))
    worker_logs._start(task_id="encode", task=task)
    (job,) = (job for job in job_progress._jobs.values() if job.task_id == "encode")

    tick(job.started + FLUSH_INTERVAL)

    row = WorkerJobLog.objects.get(task_id="encode")
    assert (row.peak_memory, row.peak_program, row.peak_program_memory, row.finished_at) == (
        300 * MB,
        "ffmpeg",
        900 * MB,
        None,
    )

    # The encode has ended, and storing its copy took the worker itself up to 1.4 GB.
    readings["own"], readings["program"] = 1434 * MB, None
    worker_logs._finish(task_id="encode", task=task, state="SUCCESS")

    row.refresh_from_db()
    assert (row.peak_memory, row.peak_program, row.peak_program_memory) == (1434 * MB, "ffmpeg", 900 * MB)


@pytest.mark.django_db
def test_memory_that_grows_by_less_than_a_mib_is_not_written_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readings = [300 * MB, 300 * MB + 4096]
    monkeypatch.setattr(job_memory, "own_peak", lambda: readings[0])
    monkeypatch.setattr(job_memory, "largest_program", lambda: None)
    job = job_progress.LiveJob("growing", timezone.now(), [])
    WorkerJobLog.objects.create(task_id="growing", started_at=job.started_at)
    job_progress.start(job, monitor=False)
    try:
        tick(job.started + FLUSH_INTERVAL)
        written_at = job.written_at
        readings.pop(0)
        tick(job.started + 3 * FLUSH_INTERVAL)
    finally:
        job_progress.finish(job.task_id)

    assert job.written_at == written_at
    assert WorkerJobLog.objects.get(task_id="growing").peak_memory == 300 * MB


@pytest.mark.django_db
def test_a_jobs_page_shows_its_peak_memory(client: Client, make_resident: Callable[..., Resident]) -> None:
    started = timezone.now() - timedelta(hours=3)
    WorkerJobLog.objects.create(
        task_id="measured",
        task_name="photo_album.tasks.build_media_derivatives",
        state="SUCCESS",
        started_at=started,
        finished_at=started + timedelta(hours=3),
        peak_memory=1434 * MB,
        peak_program="ffmpeg",
        peak_program_memory=900 * MB,
    )
    WorkerJobLog.objects.create(
        task_id="unmeasured", task_name="core.tasks.x", state="SUCCESS", started_at=started
    )
    client.force_login(make_resident(roles=(Role.ADMINISTRATOR,)))

    page = client.get(reverse("siteadmin:worker_job_detail", args=["measured"])).content.decode()
    assert "Hukommelse, maks." in page
    assert "1,4\xa0GB (worker'en) · 900,0\xa0MB (ffmpeg)" in page

    page = client.get(reverse("siteadmin:worker_job_detail", args=["unmeasured"])).content.decode()
    assert "<dt>Hukommelse, maks.</dt><dd>–</dd>" in page
