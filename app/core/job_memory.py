"""The peak memory of a worker job: its own process, and the largest program it ran (ffmpeg).

Shown on the job's page, so that a job that runs the box short of memory can be told from the ones
around it, and the next limit set from what jobs use rather than from guesses. On 2026-11-15 a
photo-album video took the worker to 1.4 GB, and nothing said which job it was.

Read from /proc, so only on Linux, in the worker's container. Elsewhere (a Mac running the tests or a
local worker) every reading is None and nothing is recorded.

THE JOB'S OWN PEAK is the kernel's high-water mark for the worker process (VmHWM), reset as the job
starts, so it is this job's and not that of the largest job the process ever ran. It is exact, and
read once more as the job ends.

A PROGRAM'S PEAK is sampled: the monitor (core/job_progress.py) reads the high-water mark of each
program the process has started, every few seconds, and keeps the largest. A program's mark covers
its whole life up to the reading, so only its last few seconds, or a program shorter than that, can
be missed, never the encode that runs for hours.

Both are measured per worker process, not per job. In production each process runs one job at a time
(--concurrency=1); a thread pool, as on a Mac, would mix up the jobs it runs side by side.
"""

import os
from pathlib import Path

PROC = Path("/proc")


def reset() -> None:
    """Start the process's high-water mark again from what it holds now (clear_refs "5")."""
    try:
        (PROC / str(os.getpid()) / "clear_refs").write_text("5")
    except OSError:
        pass


def own_peak() -> int | None:
    """The most memory, in bytes, this process has held since the last `reset`."""
    return _high_water_mark(os.getpid())


def largest_program() -> tuple[str, int] | None:
    """The name and the peak memory, in bytes, of the largest program running under this process."""
    largest: tuple[str, int] | None = None
    for pid in _descendants(os.getpid()):
        peak = _high_water_mark(pid)
        if peak is not None and (largest is None or peak > largest[1]):
            largest = (_name(pid), peak)
    return largest


def _high_water_mark(pid: int) -> int | None:
    try:
        status = (PROC / str(pid) / "status").read_text()
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) * 1024  # the kernel writes kB
    return None


def _name(pid: int) -> str:
    try:
        return (PROC / str(pid) / "comm").read_text().strip()
    except OSError:
        return ""


def _descendants(root: int) -> list[int]:
    """Every process below `root`: its children, theirs, and so on."""
    children: dict[int, list[int]] = {}
    for entry in PROC.glob("[0-9]*"):
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue  # it ended since the listing
        # "pid (name) state ppid …": the name may itself hold spaces and brackets.
        fields = stat.rpartition(")")[2].split()
        if len(fields) > 1 and fields[1].isdigit():
            children.setdefault(int(fields[1]), []).append(int(entry.name))
    found: list[int] = []
    waiting = [root]
    while waiting:
        for child in children.get(waiting.pop(), []):
            found.append(child)
            waiting.append(child)
    return found
