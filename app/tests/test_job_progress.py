"""A running job's progress and log as it goes, and the watchdog that stops one that stalls."""

import logging
import signal
import subprocess  # nosec B404
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.utils import timezone

from core import job_progress, worker_logs
from core.job_progress import FLUSH_INTERVAL, MIN_GAIN, WINDOW, LiveJob, report_progress, stage, stalled, tick
from core.models import WorkerJobLog

logger = logging.getLogger("photo_album.tasks")


@pytest.fixture
def running() -> Iterator[LiveJob]:
    """A job registered as the worker would register it, in this thread, started long ago."""
    # A whole second: the times below are the start plus whole minutes, and on a fractional
    # monotonic start, 119 minutes minus 59 could come out a hair under the hour.
    job = LiveJob("job-1", timezone.now(), [], started=float(int(time.monotonic())))
    job_progress.start(job, monitor=False)
    try:
        yield job
    finally:
        job_progress.finish(job.task_id)


def _at(job: LiveJob, minutes: float, fraction: float) -> float:
    """Report `fraction` at `minutes` after the job's start, and let the monitor see it then."""
    now = job.started + minutes * 60
    report_progress(fraction)
    job_progress._sample(job, now)
    return now


# --- Reporting --------------------------------------------------------------------------------


def test_reporting_outside_a_job_does_nothing() -> None:
    report_progress(0.5, "Konverterer")  # no job: nothing to fail, nothing to record


def test_a_stage_is_a_part_of_the_whole(running: LiveJob) -> None:
    encode = stage(0.10, 0.90, "Konverterer")
    encode(0.5)
    assert (running.fraction, running.step) == (pytest.approx(0.5), "Konverterer")
    encode(2.0)  # ffmpeg's position can overshoot the probed duration
    assert running.fraction == pytest.approx(0.9)


@pytest.mark.django_db
def test_the_log_and_progress_are_written_while_the_job_runs_and_once_at_its_end() -> None:
    """Lines used to reach the row only as the job ended: a running job showed none, and one that
    was killed lost all of them."""
    task = SimpleNamespace(
        name="photo_album.tasks.build_media_derivatives", request=SimpleNamespace(is_eager=True)
    )
    worker_logs._start(task_id="job-2", task=task)
    logger.info("Encoding clip.mov: 10%")
    report_progress(0.25, "Konverterer")
    (job,) = (job for job in job_progress._jobs.values() if job.task_id == "job-2")

    tick(job.started + FLUSH_INTERVAL)

    row = WorkerJobLog.objects.get(task_id="job-2")
    assert [line["message"] for line in row.lines] == ["Encoding clip.mov: 10%"]
    assert (row.progress, row.progress_step, row.finished_at) == (0.25, "Konverterer", None)

    logger.info("Encoding clip.mov: 20%")
    report_progress(0.95, "Gemmer versionerne")  # its last report need not be 100 %
    worker_logs._finish(task_id="job-2", task=task, state="SUCCESS")

    row.refresh_from_db()
    # Written in full at the end: the line the monitor wrote is not there twice.
    assert [line["message"] for line in row.lines] == ["Encoding clip.mov: 10%", "Encoding clip.mov: 20%"]
    assert (row.progress, row.progress_step, row.state) == (1.0, "", "SUCCESS")  # done, not stuck at 95 %
    # A late write by the monitor cannot overwrite the job's own last word.
    job.lines.append({"at": "", "level": "INFO", "logger": "x", "message": "late"})
    job_progress.write(job, job.started + 2 * FLUSH_INTERVAL)
    row.refresh_from_db()
    assert len(row.lines) == 2


@pytest.mark.django_db
@pytest.mark.django_db
def test_a_failed_job_keeps_where_it_stopped() -> None:
    task = SimpleNamespace(name="gahkflix.tasks.process_video_file", request=SimpleNamespace(is_eager=True))
    worker_logs._start(task_id="job-3", task=task)
    report_progress(0.4, "Behandler filmen")
    worker_logs._finish(task_id="job-3", task=task, state="FAILURE")

    row = WorkerJobLog.objects.get(task_id="job-3")
    assert (row.progress, row.progress_step) == (0.4, "Behandler filmen")


@pytest.mark.django_db
def test_nothing_is_written_until_something_changed(running: LiveJob) -> None:
    WorkerJobLog.objects.create(task_id=running.task_id, started_at=running.started_at)
    report_progress(0.3)
    tick(running.started + FLUSH_INTERVAL)
    WorkerJobLog.objects.filter(task_id=running.task_id).update(progress=None)  # to see a second write

    tick(running.started + 2 * FLUSH_INTERVAL)

    assert WorkerJobLog.objects.get(task_id=running.task_id).progress is None


# --- The watchdog -------------------------------------------------------------------------------


def test_a_job_is_not_judged_in_its_first_hour(running: LiveJob) -> None:
    now = _at(running, 59, 0.0)
    assert stalled(running, now) == ""


def test_a_job_is_judged_from_its_first_report_not_its_start(running: LiveJob) -> None:
    """An import downloads its ZIP before it reports anything; that hour is not held against it."""
    _at(running, 59, 0.0)
    assert stalled(running, _at(running, 61, 0.05)) == ""
    assert "0% to 5% in the last 60 minutes" in stalled(running, _at(running, 119, 0.05))


def test_a_job_that_gains_less_than_a_tenth_in_an_hour_is_stalled(running: LiveJob) -> None:
    for minutes, fraction in ((0, 0.0), (30, 0.20), (61, 0.20), (90, 0.25), (121, 0.29)):
        now = _at(running, minutes, fraction)

    assert "20% to 29% in the last" in stalled(running, now)


def test_a_job_that_keeps_its_pace_runs_for_as_long_as_it_takes(running: LiveJob) -> None:
    """Ten hours at a tenth an hour: the twelve-hour limit, not the watchdog, is its ceiling."""
    for minutes in range(0, 600, 5):
        now = _at(running, minutes, minutes / 600 + 0.001)
        assert stalled(running, now) == "", minutes


def test_a_job_with_less_than_a_tenth_left_must_finish_it_within_the_hour(running: LiveJob) -> None:
    _at(running, 0, 0.0)
    _at(running, 50, 0.96)
    assert "(or finish)" in stalled(running, _at(running, 111, 0.99))
    assert stalled(running, _at(running, 112, 1.0)) == ""


def test_a_job_that_reports_no_progress_is_not_watched(running: LiveJob) -> None:
    assert stalled(running, running.started + 10 * WINDOW) == ""


@pytest.mark.django_db
def test_a_stalled_job_is_stopped_as_if_by_its_soft_time_limit(running: LiveJob) -> None:
    """SIGUSR1 is how the prefork pool raises SoftTimeLimitExceeded in a job; a handler stands in."""
    received: list[int] = []
    previous = signal.signal(signal.SIGUSR1, lambda signum, _frame: received.append(signum))
    try:
        for minutes, fraction in ((0, 0.0), (61, MIN_GAIN / 2)):
            _at(running, minutes, fraction)
        tick(running.started + 61 * 60)
        deadline = time.monotonic() + 2
        while not received and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        signal.signal(signal.SIGUSR1, previous)

    assert received == [signal.SIGUSR1]
    assert running.stopped.startswith("Stopped by the watchdog")
    assert running.lines[-1]["level"] == "ERROR"  # in the job's log, for the page
    assert job_progress.stopped_reason() == running.stopped
    tick(running.started + 62 * 60)
    assert received == [signal.SIGUSR1]  # once


# --- ffmpeg -------------------------------------------------------------------------------------


def _clip(directory: Path, seconds: int) -> Path:
    import imageio_ffmpeg

    clip = directory / "clip.mp4"
    subprocess.run(  # nosec B603
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            *(
                "-y",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=duration={seconds}:size=160x120:rate=10",
            ),
            *("-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)),
        ],
        check=True,
    )
    return clip


def test_an_encode_reports_how_far_through_the_video_it_is(tmp_path: Path) -> None:
    from photo_album.services import _ffmpeg, _run_ffmpeg_with_progress, probe_video

    clip = _clip(tmp_path, 4)
    duration = probe_video(_ffmpeg(), str(clip)).duration
    seen: list[float] = []

    _run_ffmpeg_with_progress(_ffmpeg(), duration, seen.append, "-i", str(clip), str(tmp_path / "out.mp4"))

    assert duration == pytest.approx(4.0)
    assert seen == sorted(seen) and seen[-1] == pytest.approx(1.0, abs=0.1)


def test_an_encode_left_by_an_exception_takes_ffmpeg_with_it(tmp_path: Path) -> None:
    """The watchdog's SoftTimeLimitExceeded arrives in the middle of the encode."""
    from celery.exceptions import SoftTimeLimitExceeded

    from photo_album.services import _ffmpeg, _run_ffmpeg_with_progress

    clip = _clip(tmp_path, 4)
    started: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        started.append(real_popen(*args, **kwargs))  # type: ignore[call-overload]
        return started[-1]

    def stop(_: float) -> None:
        raise SoftTimeLimitExceeded

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(subprocess, "Popen", popen)
        with pytest.raises(SoftTimeLimitExceeded):
            _run_ffmpeg_with_progress(_ffmpeg(), 4.0, stop, "-re", "-i", str(clip), str(tmp_path / "out.mp4"))

    assert started[0].returncode is not None  # killed and reaped, not left encoding


def test_the_duration_is_read_from_ffmpegs_report() -> None:
    from photo_album.services import VideoProbe

    assert VideoProbe("  Duration: 01:02:03.50, start: 0.0").duration == pytest.approx(3723.5)
    assert VideoProbe("  Duration: N/A, bitrate: N/A").duration is None


def test_the_logged_line_tells_how_far_an_encode_has_got(caplog: pytest.LogCaptureFixture) -> None:
    from photo_album.services import _EncodeLog

    seen: list[float] = []
    progress = _EncodeLog("clip.mov", 600.0, seen.append)
    with caplog.at_level(logging.INFO, logger="photo_album.services"):
        for fraction in (0.05, 0.12, 0.13, 0.31):
            progress(fraction)

    assert seen == [0.05, 0.12, 0.13, 0.31]
    assert [record.getMessage().split(" after")[0] for record in caplog.records] == [
        "Encoding clip.mov: 10% (0:01:12 of 0:10:00)",
        "Encoding clip.mov: 30% (0:03:06 of 0:10:00)",
    ]


def test_the_watchdogs_window_and_pace() -> None:
    """Ten hours to go from start to end at the slowest pace allowed, inside the twelve-hour limit."""
    from photo_album.tasks import ENCODE_TASK_TIME_LIMIT, ZIP_TASK_TIME_LIMIT

    slowest = timedelta(seconds=WINDOW / MIN_GAIN)
    assert slowest < timedelta(seconds=ENCODE_TASK_TIME_LIMIT) == timedelta(seconds=ZIP_TASK_TIME_LIMIT)


def test_a_job_hears_from_a_thread_it_started(running: LiveJob) -> None:
    """boto3 reads a download's ZIP stream in a thread of its own, which has no job of its own."""
    import threading

    from photo_album.progress import JobProgress

    progress = JobProgress(WorkerJobLog.objects.none())
    worker = threading.Thread(target=progress, args=("zipping", 3, 4))
    worker.start()
    worker.join()

    assert (running.fraction, running.step) == (pytest.approx(0.75), "Pakker ZIP-filen")
