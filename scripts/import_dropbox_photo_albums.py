#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "dropbox>=12.0",
#     "requests>=2.32",
# ]
# ///
"""Import the photo archive in Dropbox (/GAHK billeder/<year>/<album>) into the photo album.

One album at a time: list it in Dropbox, check it against the server's own import rules, download it
as a ZIP, send it to the system import endpoint, wait for the result, delete the ZIP. Sub-folders
become sub-albums (up to the server's depth limit). The server imports an archive completely or
not at all, so a failed album leaves nothing half-imported behind.

Setup:
    export PHOTO_ALBUM_IMPORT_URL=https://<host>/intern/fotoalbum/importer-zip/system
    export PHOTO_ALBUM_IMPORT_TOKEN=<the server's PHOTO_ALBUM_IMPORT_TOKEN>
    export DROPBOX_ACCESS_TOKEN=<token>
    # ...or, for a run longer than a short-lived access token lasts:
    export DROPBOX_REFRESH_TOKEN=<token> DROPBOX_APP_KEY=<key> DROPBOX_APP_SECRET=<secret>

Usage:
    uv run scripts/import_dropbox_photo_albums.py --dry-run          # what would happen
    uv run scripts/import_dropbox_photo_albums.py --year-range 2001-2004
    uv run scripts/import_dropbox_photo_albums.py --year 2019 --confirm

Resuming: progress is saved in <work-dir>/state.json after every step, so stopping (Ctrl-C, a lost
connection, a reboot) is safe — run the same command again and it continues where it stopped. An
album already on the server is skipped; one the server was still importing is waited for rather
than sent twice. Albums that failed are skipped on later runs until you fix them in Dropbox and pass
--retry-failed. Everything printed is also appended to <work-dir>/import.log.
"""

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, TextIO, TypeVar
from urllib.parse import urljoin

import dropbox
import requests
from dropbox.exceptions import (
    ApiError,
    AuthError,
    HttpError,
    InternalServerError,
    RateLimitError,
)

YEAR = re.compile(r"^\d{4}$")
OTHER_FOLDER = "Andet"  # the server's one non-year folder
CHUNK_SIZE = 1024 * 1024
RETRIES = 5
QUEUED_TOO_LONG = 5 * 60  # seconds before a job that never starts gets a hint
# What the server ignores in an archive (photo_album.services); everything else must be media.
MACOS_METADATA_FOLDERS = {"__MACOSX", ".AppleDouble"}
MACOS_METADATA_FILES = {".DS_Store", "Icon\r"}
WINDOWS_METADATA_FILES = {"thumbs.db", "desktop.ini"}

T = TypeVar("T")


class Fatal(Exception):
    """Stops the whole run: nothing after it could succeed either (a rejected token, a full disk)."""


class AlbumFailed(Exception):
    """Stops one album; the run moves on to the next."""

    def __init__(self, summary: str, details: list[str] | None = None) -> None:
        super().__init__(summary)
        self.summary = summary
        self.details = details or []


# --- Output -------------------------------------------------------------------------------------


class Console:
    """Prints, and keeps a copy of everything in the work directory's log."""

    def __init__(self) -> None:
        self.log_file: Any = None
        self.colour = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def open_log(self, path: Path) -> None:
        self.log_file = path.open("a", encoding="utf-8")
        self.log_file.write(
            f"\n===== {datetime.now(UTC).isoformat(timespec='seconds')} {' '.join(sys.argv)}\n"
        )

    def _write(
        self, text: str, *, stream: TextIO | None = None, colour: str = ""
    ) -> None:
        stream = stream or sys.stdout
        shown = (
            f"\033[{colour}m{text}\033[0m"
            if colour and self.colour and stream.isatty()
            else text
        )
        print(shown, file=stream, flush=True)
        if self.log_file:
            self.log_file.write(text + "\n")
            self.log_file.flush()

    def info(self, text: str) -> None:
        self._write(text)

    def ok(self, text: str) -> None:
        self._write(text, colour="32")

    def warn(self, text: str) -> None:
        self._write(text, colour="33")

    def error(self, text: str) -> None:
        self._write(text, stream=sys.stderr, colour="1;31")

    def heading(self, text: str) -> None:
        self._write(f"\n{text}", colour="1")


out = Console()


def die(message: str) -> NoReturn:
    out.error(f"\n✗ {message}")
    raise SystemExit(2)


# --- Progress -----------------------------------------------------------------------------------


class State:
    """What has happened to each Dropbox album folder, keyed by its Dropbox path.

    status: "submitted" (sent; the server may still be importing — statusUrl says where to ask),
    "imported", "failed". Albums that are already on the server are not recorded: the server is
    asked on every run, so an album deleted on the site is offered again.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.albums: dict[str, dict[str, Any]] = {}
        if path.exists():
            try:
                self.albums = json.loads(path.read_text(encoding="utf-8"))["albums"]
            except (ValueError, KeyError) as exc:
                die(
                    f"{path} is not a state file this script wrote ({exc}). Move it away to start over."
                )

    def get(self, key: str) -> dict[str, Any]:
        return self.albums.get(key, {})

    def set(self, key: str, **entry: object) -> None:
        self.albums[key] = {
            **entry,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        partial = self.path.with_suffix(".json.partial")
        partial.write_text(
            json.dumps({"albums": self.albums}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        partial.replace(
            self.path
        )  # atomic: a crash mid-write never leaves a broken state file


# --- The server ---------------------------------------------------------------------------------


@dataclass
class Rules:
    """What the server accepts in an archive, as it reports it (photo_album.views.system_albums)."""

    image_extensions: frozenset[str]
    video_extensions: frozenset[str]
    image_max_bytes: int
    video_max_bytes: int
    max_sub_album_levels: int
    macos_metadata_max_bytes: int

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Rules":
        return cls(
            image_extensions=frozenset(data["imageExtensions"]),
            video_extensions=frozenset(data["videoExtensions"]),
            image_max_bytes=int(data["imageMaxMb"]) * 1024 * 1024,
            video_max_bytes=int(data["videoMaxMb"]) * 1024 * 1024,
            max_sub_album_levels=int(data["maxSubAlbumLevels"]),
            macos_metadata_max_bytes=int(data["macosMetadataMaxBytes"]),
        )


def _transient(response: requests.Response) -> bool:
    return response.status_code == 429 or response.status_code >= 500


class Server:
    def __init__(self, endpoint: str, token: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        if not self.endpoint.endswith("/system"):
            self.endpoint += "/system"
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        )

    def _request(
        self, method: str, url: str, *, what: str, retry: bool = True, **kwargs: object
    ) -> requests.Response:
        for attempt in range(1, RETRIES + 1):
            try:
                response = self.session.request(method, url, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                problem = f"no answer ({exc.__class__.__name__})"
            else:
                if response.status_code in (401, 403):
                    raise Fatal(
                        f"The server refused the import token ({response.status_code}) while trying to {what}. "
                        "Check PHOTO_ALBUM_IMPORT_TOKEN against the server's setting."
                    )
                if not _transient(response):
                    return response
                problem = f"HTTP {response.status_code}"
            if not retry or attempt == RETRIES:
                raise AlbumFailed(f"Could not {what}: {problem}.")
            wait = 2**attempt
            out.warn(
                f"    … {what}: {problem}; retrying in {wait}s ({attempt}/{RETRIES - 1})"
            )
            time.sleep(wait)
        raise AssertionError("unreachable")

    def albums(self, folder: str) -> tuple[set[str], Rules]:
        """The names (compared as the server does) of the root albums already in `folder`, and the rules."""
        response = self._request(
            "GET",
            f"{self.endpoint}/albummer",
            what="list existing albums",
            params={"folder": folder},
            timeout=60,
        )
        if response.status_code == 404:
            raise Fatal(
                f"{self.endpoint}/albummer does not exist. Is PHOTO_ALBUM_IMPORT_URL right, and is the server "
                "running a version with sub-albums?"
            )
        response.raise_for_status()
        data = response.json()
        return {same_name(album["name"]) for album in data["albums"]}, Rules.from_json(
            data["rules"]
        )

    def submit(self, archive: Path, folder: str) -> str:
        """Send the ZIP; returns the absolute status URL.

        Not retried on a dropped connection: the server may have received it, and a second copy would
        only be refused as a duplicate album, hiding whether the first one worked. The next run checks.
        """
        with archive.open("rb") as handle:
            response = self._request(
                "POST",
                self.endpoint,
                what=f"send {archive.name}",
                retry=False,
                data={"folder": folder},
                files={"archive": (archive.name, handle, "application/zip")},
                timeout=(30, None),
            )
        if response.status_code == 400:
            raise AlbumFailed("The server refused the upload.", form_errors(response))
        if response.status_code == 413:
            raise AlbumFailed(
                f"The ZIP ({human_size(archive.stat().st_size)}) is larger than the server or its proxy accepts."
            )
        response.raise_for_status()
        return urljoin(self.endpoint, response.json()["statusUrl"])

    def status(self, url: str) -> dict[str, Any]:
        response = self._request(
            "GET", url, what="ask for the import status", timeout=60
        )
        if response.status_code == 404:
            raise AlbumFailed(
                "The server no longer knows this import (status URL gives 404)."
            )
        response.raise_for_status()
        return response.json()


def form_errors(response: requests.Response) -> list[str]:
    try:
        errors = response.json()["errors"]
    except (ValueError, KeyError):
        return [response.text[:500]]
    return [
        f"{field_name}: {error['message']}"
        for field_name, items in errors.items()
        for error in items
    ]


# --- Dropbox ------------------------------------------------------------------------------------


def dropbox_client() -> dropbox.Dropbox:
    refresh_token = os.environ.get("DROPBOX_REFRESH_TOKEN")
    if refresh_token:
        app_key = os.environ.get("DROPBOX_APP_KEY") or die(
            "DROPBOX_REFRESH_TOKEN needs DROPBOX_APP_KEY as well."
        )
        return dropbox.Dropbox(
            oauth2_refresh_token=refresh_token,
            app_key=app_key,
            app_secret=os.environ.get("DROPBOX_APP_SECRET"),
        )
    access_token = os.environ.get("DROPBOX_ACCESS_TOKEN") or die(
        "Set DROPBOX_ACCESS_TOKEN (or DROPBOX_REFRESH_TOKEN with DROPBOX_APP_KEY)."
    )
    return dropbox.Dropbox(access_token)


def with_dropbox_retries[T](what: str, call: Callable[[], T]) -> T:
    for attempt in range(1, RETRIES + 1):
        try:
            return call()
        except AuthError as exc:
            raise Fatal(
                f"Dropbox refused the token while trying to {what}: {exc.error}"
            ) from exc
        except RateLimitError as exc:
            wait = exc.backoff or 2**attempt
            problem = "rate limited"
        except (
            InternalServerError,
            HttpError,
            requests.ConnectionError,
            requests.Timeout,
        ) as exc:
            wait = 2**attempt
            problem = exc.__class__.__name__
        if attempt == RETRIES:
            raise AlbumFailed(f"Dropbox: could not {what} ({problem}).")
        out.warn(
            f"    … Dropbox: {what}: {problem}; retrying in {wait}s ({attempt}/{RETRIES - 1})"
        )
        time.sleep(wait)
    raise AssertionError("unreachable")


def list_folder(
    client: dropbox.Dropbox, path: str, *, recursive: bool = False
) -> Iterator[Any]:
    try:
        listing = with_dropbox_retries(
            f"list {path}",
            lambda: client.files_list_folder(
                path, recursive=recursive, include_non_downloadable_files=False
            ),
        )
    except ApiError as exc:
        raise AlbumFailed(f"Dropbox cannot list {path}: {exc.error}") from exc
    yield from listing.entries
    while listing.has_more:
        listing = with_dropbox_retries(
            f"list {path}",
            lambda cursor=listing.cursor: client.files_list_folder_continue(cursor),
        )
        yield from listing.entries


def download_zip(
    client: dropbox.Dropbox, source: str, target: Path, expected_files: int
) -> None:
    """Download `source` as a ZIP to `target`, all of it or nothing: it is written aside and checked first."""
    partial = target.with_name(target.name + ".partial")
    for attempt in range(1, RETRIES + 1):
        try:
            _, response = with_dropbox_retries(
                f"start the ZIP of {source}", lambda: client.files_download_zip(source)
            )
            written = 0
            last_report = time.monotonic()
            with response, partial.open("wb") as destination:
                for chunk in response.iter_content(CHUNK_SIZE):
                    destination.write(chunk)
                    written += len(chunk)
                    if time.monotonic() - last_report > 15:
                        out.info(f"    … {human_size(written)} downloaded")
                        last_report = time.monotonic()
            break
        except ApiError as exc:
            partial.unlink(missing_ok=True)
            reason = str(exc.error)
            if "too_large" in reason or "too_many_files" in reason:
                raise AlbumFailed(
                    "Dropbox will not ZIP this folder: it is over 20 GB or 10,000 files. Split it into "
                    "sub-folders in Dropbox, or import those separately."
                ) from exc
            raise AlbumFailed(f"Dropbox refused to ZIP {source}: {reason}") from exc
        except (
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ) as exc:
            partial.unlink(missing_ok=True)
            if attempt == RETRIES:
                raise AlbumFailed(
                    f"The download of {source} kept breaking off ({exc.__class__.__name__})."
                ) from exc
            out.warn(
                f"    … download broke off; starting it again ({attempt}/{RETRIES - 1})"
            )
            time.sleep(2**attempt)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise Fatal(f"Cannot write {partial}: {exc}") from exc
    try:
        with zipfile.ZipFile(partial) as archive:
            broken = archive.testzip()
            files = sum(1 for member in archive.infolist() if not member.is_dir())
    except zipfile.BadZipFile as exc:
        partial.unlink(missing_ok=True)
        raise AlbumFailed(f"Dropbox sent a ZIP that cannot be read ({exc}).") from exc
    if broken is not None or files != expected_files:
        partial.unlink(missing_ok=True)
        problem = (
            f"{broken} is damaged"
            if broken
            else f"it holds {files} files where Dropbox lists {expected_files}"
        )
        raise AlbumFailed(
            f"The downloaded ZIP is not right: {problem}. Run again to retry."
        )
    partial.replace(target)


# --- Checking an album before downloading it ----------------------------------------------------


@dataclass
class AlbumPlan:
    name: str  # the root album the server will create
    files: int = 0  # media files
    total_files: int = 0  # everything in the folder, which is what the ZIP will hold
    size: int = 0
    sub_albums: set[tuple[str, ...]] = field(default_factory=set)
    ignored: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def same_name(name: str) -> str:
    """Album names as the server compares them: NFC, case-insensitive."""
    return unicodedata.normalize("NFC", name).casefold()


def plan_album(
    folder_name: str, entries: list[Any], album_path: str, rules: Rules
) -> AlbumPlan:
    """What importing this Dropbox folder would do, judged by the same rules the server applies.

    The ZIP Dropbox builds wraps everything in `folder_name/`, and the server descends through any
    level holding exactly one folder and no files, naming the album after where it stops — so a
    folder "Sommerfest" holding only "Billeder/" becomes an album named "Billeder".
    """
    files: list[tuple[tuple[str, ...], int]] = []
    prefix_length = len(album_path.rstrip("/")) + 1
    for entry in entries:
        if not isinstance(entry, dropbox.files.FileMetadata):
            continue
        relative = entry.path_display[prefix_length:]
        files.append(((folder_name, *PurePosixPath(relative).parts), entry.size))

    plan = AlbumPlan(name=folder_name, total_files=len(files))
    media: list[tuple[str, ...]] = []
    for parts, size in files:
        shown = "/".join(parts[1:])
        name = parts[-1]
        if (
            MACOS_METADATA_FOLDERS & set(parts)
            or name.startswith("._")
            or name in MACOS_METADATA_FILES
        ):
            if size <= rules.macos_metadata_max_bytes:
                plan.ignored.append(f"{shown} (macOS metadata)")
            else:
                plan.problems.append(
                    f"{shown}: looks like macOS metadata but is {human_size(size)}"
                )
            continue
        if name.lower() in WINDOWS_METADATA_FILES:
            plan.ignored.append(f"{shown} (Windows system file)")
            continue
        extension = PurePosixPath(name).suffix.lower()
        if extension in rules.image_extensions:
            limit = rules.image_max_bytes
        elif extension in rules.video_extensions:
            limit = rules.video_max_bytes
        else:
            plan.problems.append(
                f"{shown}: not a photo or video the album accepts ({extension or 'no extension'})"
            )
            continue
        if size > limit:
            plan.problems.append(
                f"{shown}: {human_size(size)} is over the {human_size(limit)} limit"
            )
            continue
        media.append(parts)
        plan.files += 1
        plan.size += size

    prefix: tuple[str, ...] = ()
    while media:
        level = [parts[len(prefix) :] for parts in media]
        if (
            any(len(rest) == 1 for rest in level)
            or len({rest[0] for rest in level}) != 1
        ):
            break
        prefix = (*prefix, level[0][0])
    if prefix:
        plan.name = prefix[-1]
    for parts in media:
        folders = parts[len(prefix) : -1]
        if len(folders) > rules.max_sub_album_levels:
            plan.problems.append(
                f"{'/'.join(parts[1:])}: more than {rules.max_sub_album_levels} folder levels below the album"
            )
        for depth in range(1, len(folders) + 1):
            plan.sub_albums.add(folders[:depth])
    if not plan.files and not plan.problems:
        plan.problems.append("no photos or videos in it")
    return plan


# --- One album ----------------------------------------------------------------------------------


@dataclass
class Options:
    work_dir: Path
    poll_interval: float
    dry_run: bool
    confirm: bool
    retry_failed: bool


def import_album(
    client: dropbox.Dropbox,
    server: Server,
    state: State,
    options: Options,
    *,
    year: str,
    folder_name: str,
    album_path: str,
    existing: set[str],
    rules: Rules,
) -> str:
    """Take one Dropbox folder as far as it will go. Returns its final status for the summary."""
    previous = state.get(album_path)
    if previous.get("status") == "imported":
        out.ok(
            f'  ✓ Imported on an earlier run as "{year}/{previous.get("album") or folder_name}".'
        )
        return "done"
    if previous.get("status") == "submitted":
        out.info("  Sent on an earlier run; asking the server how it went …")
        return wait_for_import(server, state, options, album_path, previous)
    if previous.get("status") == "failed" and not options.retry_failed:
        out.warn(
            "  Failed on an earlier run — skipped. Fix it in Dropbox, then rerun with --retry-failed."
        )
        for line in previous.get("errors", [])[:5]:
            out.warn(f"      {line}")
        return "failed"

    entries = list(list_folder(client, album_path, recursive=True))
    plan = plan_album(folder_name, entries, album_path, rules)
    if plan.problems:
        raise AlbumFailed(
            f"{len(plan.problems)} problem(s) in Dropbox — the server would refuse the whole album:",
            plan.problems,
        )
    if same_name(plan.name) in existing:
        out.ok(f'  ✓ Already on the server as "{year}/{plan.name}" — skipped.')
        return "exists"

    renamed = f' as album "{plan.name}"' if plan.name != folder_name else ""
    sub_albums = f", {len(plan.sub_albums)} sub-album(s)" if plan.sub_albums else ""
    out.info(f"  {plan.files} file(s), {human_size(plan.size)}{sub_albums}{renamed}")
    for line in plan.ignored:
        out.info(f"    ignored: {line}")
    if options.dry_run:
        return "would import"
    if options.confirm and not ask(f"  Import {year}/{folder_name}?"):
        out.info("  Skipped (you said no).")
        return "declined"

    target_dir = options.work_dir / year
    target_dir.mkdir(parents=True, exist_ok=True)
    archive = target_dir / f"{plan.name.replace('/', '_')}.zip"
    out.info("  Downloading from Dropbox …")
    download_zip(client, album_path, archive, plan.total_files)
    try:
        out.info(f"  Sending {human_size(archive.stat().st_size)} to the server …")
        status_url = server.submit(archive, year)
        state.set(album_path, status="submitted", album=plan.name, statusUrl=status_url)
        return wait_for_import(
            server, state, options, album_path, state.get(album_path)
        )
    finally:
        archive.unlink(
            missing_ok=True
        )  # Dropbox still has it; the disk should not fill up with retries


def wait_for_import(
    server: Server,
    state: State,
    options: Options,
    album_path: str,
    entry: dict[str, Any],
) -> str:
    started = time.monotonic()
    last_state = None
    last_report = started
    while True:
        job = server.status(entry["statusUrl"])
        if job["state"] != last_state:
            out.info(f"  Server: {job['state']}")
            last_state = job["state"]
        elif time.monotonic() - last_report > 60:
            out.info(
                f"    … still {job['state']} ({int(time.monotonic() - started) // 60} min)"
            )
            if (
                job["state"] == "queued"
                and time.monotonic() - started > QUEUED_TOO_LONG
            ):
                out.warn(
                    "    The server has not started on it. Its background worker may be down or busy."
                    " You can stop with Ctrl-C and run again later — this album will be picked up."
                )
            last_report = time.monotonic()
        if job["state"] == "ready":
            state.set(
                album_path,
                status="imported",
                album=entry.get("album"),
                albums=[album["path"] for album in job.get("albums", [])],
                skipped=job.get("skipped", []),
            )
            albums = job.get("albums", [])
            out.ok(
                f"  ✓ Imported: {albums[0]['path'] if albums else entry.get('album')}"
                + (f" (+{len(albums) - 1} sub-album(s))" if len(albums) > 1 else "")
            )
            for line in job.get("skipped", []):
                out.info(f"    left out: {line}")
            return "imported"
        if job["state"] == "failed":
            raise AlbumFailed(
                f"The server refused the album: {job.get('error') or 'no reason given'}",
                job.get("errors") or [],
            )
        time.sleep(options.poll_interval)


# --- Helpers ------------------------------------------------------------------------------------


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return (
                f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
            )
        value /= 1024
    raise AssertionError("unreachable")


def ask(question: str) -> bool:
    while True:
        answer = input(f"{question} [y]es/[n]o: ").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no", "s", "skip"}:
            return False
        print("Answer y or n.")


def year_range(value: str) -> set[str]:
    start, separator, end = value.partition("-")
    if not separator or not YEAR.fullmatch(start) or not YEAR.fullmatch(end):
        raise argparse.ArgumentTypeError("must be YYYY-YYYY, for example 2001-2004")
    if start > end:
        raise argparse.ArgumentTypeError("must end with the same or a later year")
    return {str(year) for year in range(int(start), int(end) + 1)}


def year_folders(client: dropbox.Dropbox, root: str, selected: set[str]) -> list[str]:
    try:
        entries = list(list_folder(client, root))
    except AlbumFailed as exc:
        raise Fatal(f"{exc.summary} Is --dropbox-root right?") from exc
    folders = sorted(
        entry.name
        for entry in entries
        if isinstance(entry, dropbox.files.FolderMetadata)
    )
    found = [name for name in folders if YEAR.fullmatch(name) or name == OTHER_FOLDER]
    for name in folders:
        if name not in found:
            out.warn(f"Ignoring {root}/{name}: not a year folder (or {OTHER_FOLDER}).")
    for entry in entries:
        if isinstance(entry, dropbox.files.FileMetadata):
            out.warn(
                f"Ignoring {entry.path_display}: files must be inside an album folder."
            )
    if selected:
        if missing := selected - set(found):
            raise Fatal(f"Not found in {root}: {', '.join(sorted(missing))}")
        found = [name for name in found if name in selected]
    return found


def album_folders(client: dropbox.Dropbox, year_path: str) -> list[Any]:
    entries = list(list_folder(client, year_path))
    for entry in entries:
        if isinstance(entry, dropbox.files.FileMetadata):
            out.warn(
                f"Ignoring {entry.path_display}: files must be inside an album folder."
            )
    folders = [
        entry for entry in entries if isinstance(entry, dropbox.files.FolderMetadata)
    ]
    return sorted(folders, key=lambda entry: entry.name)


# --- Main ---------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("PHOTO_ALBUM_IMPORT_URL"),
        help="the system import URL (default: $PHOTO_ALBUM_IMPORT_URL)",
    )
    parser.add_argument("--dropbox-root", default="/GAHK billeder")
    years = parser.add_mutually_exclusive_group()
    years.add_argument(
        "--year", action="append", default=[], help="only this year folder (repeatable)"
    )
    years.add_argument(
        "--year-range", type=year_range, help="only these years, e.g. 2001-2004"
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path(".dropbox-photo-album-import"),
        help="where the state file, the log and the ZIP in transit live",
    )
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="check every album and report; send nothing",
    )
    parser.add_argument(
        "--confirm", action="store_true", help="ask before importing each album"
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="try albums that failed on an earlier run again",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="stop at the first album that fails",
    )
    args = parser.parse_args()

    if not args.endpoint:
        die("Set PHOTO_ALBUM_IMPORT_URL or pass --endpoint.")
    token = os.environ.get("PHOTO_ALBUM_IMPORT_TOKEN") or die(
        "Set PHOTO_ALBUM_IMPORT_TOKEN."
    )
    selected = args.year_range or set(args.year)
    if invalid := sorted(
        year for year in selected if not YEAR.fullmatch(year) and year != OTHER_FOLDER
    ):
        die(f"Not a year: {', '.join(invalid)}")

    args.work_dir.mkdir(parents=True, exist_ok=True)
    out.open_log(args.work_dir / "import.log")
    state = State(args.work_dir / "state.json")
    for leftover in args.work_dir.rglob("*.partial"):
        leftover.unlink()  # a download the last run did not finish
    options = Options(
        args.work_dir, args.poll_interval, args.dry_run, args.confirm, args.retry_failed
    )
    client = dropbox_client()
    server = Server(args.endpoint, token)
    results: dict[str, list[str]] = {}

    try:
        out.info(f"Checking {server.endpoint} …")
        server.albums(
            OTHER_FOLDER
        )  # a wrong URL or token stops the run here, not after the Dropbox listing
        out.info(f"Reading {args.dropbox_root} in Dropbox …")
        for year in year_folders(client, args.dropbox_root, selected):
            year_path = f"{args.dropbox_root}/{year}"
            existing, rules = server.albums(year)
            albums = album_folders(client, year_path)
            out.heading(
                f"── {year}: {len(albums)} album folder(s), {len(existing)} album(s) already on the server"
            )
            for entry in albums:
                album_path = f"{year_path}/{entry.name}"
                out.heading(f"{year}/{entry.name}")
                try:
                    result = import_album(
                        client,
                        server,
                        state,
                        options,
                        year=year,
                        folder_name=entry.name,
                        album_path=album_path,
                        existing=existing,
                        rules=rules,
                    )
                except (AlbumFailed, requests.RequestException, ValueError) as error:
                    failure = (
                        error
                        if isinstance(error, AlbumFailed)
                        else AlbumFailed(f"Unexpected answer: {error}")
                    )
                    out.error(f"  ✗ FAILED: {failure.summary}")
                    for line in failure.details:
                        out.error(f"      {line}")
                    if not options.dry_run:
                        state.set(
                            album_path,
                            status="failed",
                            errors=[failure.summary, *failure.details],
                        )
                    result = "failed"
                    if args.stop_on_error:
                        raise Fatal(
                            "Stopped at the first failure (--stop-on-error)."
                        ) from failure
                if result == "imported":
                    existing.add(
                        same_name(state.get(album_path).get("album") or entry.name)
                    )
                results.setdefault(result, []).append(f"{year}/{entry.name}")
    except KeyboardInterrupt:
        out.error(
            "\n✗ Stopped. Progress is saved; run the same command again to continue."
        )
        return 130
    except (Fatal, AlbumFailed, requests.RequestException) as exc:
        out.error(f"\n✗ {exc}")
        out.error("  Progress is saved; run the same command again once this is fixed.")
        return 2
    finally:
        for leftover in args.work_dir.rglob("*.partial"):
            leftover.unlink(missing_ok=True)

    out.heading("Summary")
    labels = {
        "imported": "imported now",
        "would import": "would be imported (dry run)",
        "done": "imported on an earlier run",
        "exists": "already on the server",
        "declined": "skipped by you",
        "failed": "FAILED",
    }
    for key, label in labels.items():
        if results.get(key):
            out.info(f"  {len(results[key]):4d}  {label}")
    if results.get("failed"):
        out.error(f"\n✗ {len(results['failed'])} album(s) failed:")
        for name in results["failed"]:
            out.error(f"    {name}")
        if args.dry_run:
            out.error(
                "  Details are above and in the log. Fix them in Dropbox, then run again."
            )
        else:
            out.error(
                "  Details are above and in the log. Fix them in Dropbox, then rerun with --retry-failed."
            )
        return 1
    out.ok("\n✓ Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
