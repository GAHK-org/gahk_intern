"""The Docker healthcheck of each worker and beat (docker-compose.prod.yml), on the file
core/heartbeat.py touches with every heartbeat row:

    python core/heartbeat_check.py default          # exit 0 while the file is fresh, 1 otherwise
    python core/heartbeat_check.py --reset default  # at container start: drop the last run's file

Standard library only, and a script rather than a management command: it runs every 30 seconds in
three containers, and booting Django each time would cost more than the work it checks. The
constants live here so that the writer, this check and /health share one copy. Run as a script,
this directory comes first on sys.path, so nothing in core/ may share a standard-library name.
"""

import sys
import time
from pathlib import Path

# /health calls a process down once its row is this old: several missed beats, not one slow write.
HEARTBEAT_MAX_AGE = 120  # seconds
HEARTBEAT_DIR = Path("/tmp")  # noqa: S108  # nosec B108 — fixed, so the writer and the check agree


def heartbeat_file(name: str) -> Path:
    return HEARTBEAT_DIR / f"heartbeat-{name}"


def check(name: str) -> str | None:
    """None while the heartbeat is fresh, else why not (Docker keeps it in the health log)."""
    path = heartbeat_file(name)
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return f"no {path} yet"
    return None if age <= HEARTBEAT_MAX_AGE else f"{path} is {int(age)}s old"


def main(argv: list[str]) -> int:
    if argv[0] == "--reset":
        # A restart keeps the container's /tmp, and the last run's file would pass for up to
        # HEARTBEAT_MAX_AGE before this run has beaten once.
        heartbeat_file(argv[1]).unlink(missing_ok=True)
        return 0
    problem = check(argv[0])
    if problem:
        print(problem, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
