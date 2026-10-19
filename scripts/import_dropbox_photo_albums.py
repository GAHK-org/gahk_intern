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
not at all, so a failed album leaves nothing half-imported behind, and locks every album it imports
this way (Fotogruppen can unlock one that needs a change).

Files the server would refuse — anything but photos and videos it accepts, files over its size
limits or too deep — are kept out of the ZIP, so the rest of the album gets in, and saved to
<work-dir>/<year>/<album>/invalid/ (mirroring the Dropbox folder) to be looked at and fixed. So is a
file the server refuses on reading it, after which the rest is sent again. Metadata the server skips
(.DS_Store, __MACOSX/, Thumbs.db, …) is sent anyway, so the server's log shows it, and a copy is kept
in <work-dir>/<year>/<album>/metadata/.

Once the kept-out files are fixed, --retry-invalid sends them — and only them — to their album. The
server adds them to it rather than refusing an album that already exists, storing only files it does
not have (the same name with other content is a different photo and is kept). The album has to be
unlocked on the site first. Sent files move on to retried/; what is still invalid stays.

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
    uv run scripts/import_dropbox_photo_albums.py --retry-failed --year 2019  # send failed albums again
    uv run scripts/import_dropbox_photo_albums.py --retry-invalid .dropbox-photo-album-import/2019/Sommerfest

Resuming: progress is saved in <work-dir>/state.json after every step, so stopping (Ctrl-C, a lost
connection, a reboot) is safe — run the same command again and it continues where it stopped. An
album already on the server is skipped; one the server was still importing is waited for rather
than sent twice. A server that stops answering is tried again every 30 seconds, 5 times; then the
run stops, as every album after it would fail too. Albums that failed are skipped on later runs until you fix them in Dropbox and pass
--retry-failed. Everything printed is also appended to <work-dir>/import.log, and every failure —
with all its reasons — to <work-dir>/failures.log.
"""

import argparse
import json
import os
import re
import shutil
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
# A server that does not answer at all (down, restarting, its proxy finding nothing behind it) is tried
# again this often before the run gives up: every album after it would fail the same way.
UNREACHABLE_RETRIES = 5
UNREACHABLE_WAIT = 30  # seconds
QUEUED_TOO_LONG = 5 * 60  # seconds before a job that never starts gets a hint
# What the server ignores in an archive (photo_album.services); everything else must be media.
MACOS_METADATA_FOLDERS = {"__MACOSX", ".AppleDouble"}
MACOS_METADATA_FILES = {".DS_Store", "Icon\r"}
WINDOWS_METADATA_FILES = {"thumbs.db", "desktop.ini"}
# How the server reports the metadata it skipped (photo_album.services).
METADATA_SKIPPED = (": macOS-metadata (ignoreret)", ": Windows-systemfil (ignoreret)")

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
        self.failure_log: Path | None = None
        self.colour = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def open_log(self, path: Path, failure_log: Path) -> None:
        self.log_file = path.open("a", encoding="utf-8")
        self.log_file.write(
            f"\n===== {datetime.now(UTC).isoformat(timespec='seconds')} {' '.join(sys.argv)}\n"
        )
        self.failure_log = failure_log

    def failure(
        self, where: str, summary: str, details: list[str], *, echo: bool = True
    ) -> None:
        """Print a failure, and append it to the failure log — every run's, never truncated."""
        if echo:
            self.error(f"  ✗ FAILED: {summary}")
            for line in details:
                self.error(f"      {line}")
        if self.failure_log:
            with self.failure_log.open("a", encoding="utf-8") as log:
                log.write(
                    f"{datetime.now(UTC).isoformat(timespec='seconds')}  {where}\n"
                    + "".join(f"    {line}\n" for line in (summary, *details))
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
    macos_metadata_any_size: frozenset[str]

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Rules":
        return cls(
            image_extensions=frozenset(data["imageExtensions"]),
            video_extensions=frozenset(data["videoExtensions"]),
            image_max_bytes=int(data["imageMaxMb"]) * 1024 * 1024,
            video_max_bytes=int(data["videoMaxMb"]) * 1024 * 1024,
            max_sub_album_levels=int(data["maxSubAlbumLevels"]),
            macos_metadata_max_bytes=int(data["macosMetadataMaxBytes"]),
            # Absent from servers older than the .DS_Store exception, which had no such names.
            macos_metadata_any_size=frozenset(data.get("macosMetadataAnySize", [])),
        )


def _transient(response: requests.Response) -> bool:
    return response.status_code == 429 or response.status_code >= 500


# What a proxy answers when nothing is behind it: the server is down, not busy.
GATEWAY_ERRORS = {502, 503, 504}


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
        self,
        method: str,
        url: str,
        *,
        what: str,
        rewind: Callable[[], object] | None = None,
        **kwargs: object,
    ) -> requests.Response:
        """Tries again while the server is busy (the album fails if it stays so) or does not answer
        (the run stops if it stays so: every album after it would fail the same way).

        `rewind` puts an uploaded file back at its start, which the attempt before read to the end.
        """
        busy = unreachable = 0
        while True:
            if rewind:
                rewind()
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
                if response.status_code not in GATEWAY_ERRORS:
                    busy += 1
                    if busy == RETRIES:
                        raise AlbumFailed(f"Could not {what}: {problem}.")
                    wait = 2**busy
                    out.warn(
                        f"    … {what}: {problem}; retrying in {wait}s ({busy}/{RETRIES - 1})"
                    )
                    time.sleep(wait)
                    continue
            unreachable += 1
            if unreachable > UNREACHABLE_RETRIES:
                raise Fatal(
                    f"The server did not answer {UNREACHABLE_RETRIES + 1} times in a row,"
                    f" {UNREACHABLE_WAIT}s apart, while trying to {what} ({problem}). Giving up."
                )
            out.warn(
                f"    … {what}: {problem}; the server may be down — trying again in"
                f" {UNREACHABLE_WAIT}s ({unreachable}/{UNREACHABLE_RETRIES})"
            )
            time.sleep(UNREACHABLE_WAIT)

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

    def submit(self, archive: Path, folder: str, *, album: str = "") -> str:
        """Send the ZIP; returns the absolute status URL.

        The server adds it to an album of the same name if there is one, storing only files that
        album does not already have. With `album`, the ZIP's paths are relative to that album.

        Sent again if the connection fails: the server only takes an import on once the whole
        upload is in, and answers it at once, so a connection lost on the way means it has none.
        """
        with archive.open("rb") as handle:
            response = self._request(
                "POST",
                self.endpoint,
                what=f"send {archive.name}",
                rewind=lambda: handle.seek(0),
                data={"folder": folder, "album": album},
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


# --- Checking an album against the server's rules ----------------------------------------------


@dataclass
class AlbumPlan:
    name: str  # the root album the server will create, or add to
    # The folders below the Dropbox folder that the server descends through to reach that album.
    prefix: tuple[str, ...] = ()
    # Everything in the folder, which is what the ZIP from Dropbox holds.
    total_files: int = 0
    size: int = 0  # of the media
    sub_albums: set[tuple[str, ...]] = field(default_factory=set)
    # Paths below the Dropbox folder. Media is sent. Metadata the server skips is sent too, so the
    # server's log shows it was there, and a copy is kept. Invalid files — the server would refuse
    # the whole album for any one of them — are kept out of the upload, with why, so the rest gets in.
    media: list[tuple[str, ...]] = field(default_factory=list)
    metadata: dict[tuple[str, ...], str] = field(default_factory=dict)
    invalid: dict[tuple[str, ...], str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)  # about the album as a whole

    @property
    def files(self) -> int:
        return len(self.media)


def same_name(name: str) -> str:
    """Album names as the server compares them: NFC, case-insensitive."""
    return unicodedata.normalize("NFC", name).casefold()


def shown(path: tuple[str, ...]) -> str:
    return "/".join(path)


def below(path: tuple[str, ...], prefix: tuple[str, ...]) -> tuple[str, ...]:
    """`path` relative to the album the server descends to at `prefix`.

    Only metadata and kept-out files can lie outside it; they are placed from the album's root.
    """
    return path[len(prefix) :] if path[: len(prefix)] == prefix else path


def classify(path: tuple[str, ...], size: int, rules: Rules) -> tuple[str, str]:
    """("media" | "metadata" | "invalid", why) for one file, by the rules the server applies."""
    name = path[-1]
    if (
        MACOS_METADATA_FOLDERS & set(path)
        or name.startswith("._")
        or name in MACOS_METADATA_FILES
    ):
        if (
            name in rules.macos_metadata_any_size
            or size <= rules.macos_metadata_max_bytes
        ):
            return "metadata", "macOS metadata"
        return "invalid", f"looks like macOS metadata but is {human_size(size)}"
    if name.lower() in WINDOWS_METADATA_FILES:
        return "metadata", "Windows system file"
    extension = PurePosixPath(name).suffix.lower()
    if extension in rules.image_extensions:
        limit = rules.image_max_bytes
    elif extension in rules.video_extensions:
        limit = rules.video_max_bytes
    else:
        return (
            "invalid",
            f"not a photo or video the album accepts ({extension or 'no extension'})",
        )
    if size > limit:
        return "invalid", f"{human_size(size)} is over the {human_size(limit)} limit"
    return "media", ""


def descend(paths: list[tuple[str, ...]]) -> tuple[str, ...]:
    """The folders the server descends through: while a level holds one folder and no files."""
    prefix: tuple[str, ...] = ()
    while paths:
        level = [path[len(prefix) :] for path in paths]
        if (
            any(len(rest) == 1 for rest in level)
            or len({rest[0] for rest in level}) != 1
        ):
            break
        prefix = (*prefix, level[0][0])
    return prefix


def plan_files(
    folder_name: str,
    files: list[tuple[tuple[str, ...], int]],
    rules: Rules,
    *,
    pinned: tuple[str, tuple[str, ...]] | None = None,
) -> AlbumPlan:
    """What importing these files — (path below the Dropbox folder, size) — would do.

    The ZIP Dropbox builds wraps everything in `folder_name/`, and the server descends through any
    level holding exactly one folder and no files, naming the album after where it stops — so a
    folder "Sommerfest" holding only "Billeder/" becomes an album named "Billeder". Kept-out files
    count, as they would on the server: the album is the one the folder makes, and a fixed file
    sent later lands where it was. With `pinned` (name, prefix) that album is already known.
    """
    plan = AlbumPlan(name=folder_name, total_files=len(files))
    sizes: dict[tuple[str, ...], int] = {}
    for path, size in files:
        kind, why = classify(path, size, rules)
        if kind == "media":
            sizes[path] = size
        elif kind == "metadata":
            plan.metadata[path] = why
        else:
            plan.invalid[path] = why
    if pinned:
        plan.name, plan.prefix = pinned
    else:
        plan.prefix = descend([*sizes, *plan.invalid])
        plan.name = plan.prefix[-1] if plan.prefix else folder_name
    for path in [*sizes]:
        if len(below(path, plan.prefix)) - 1 > rules.max_sub_album_levels:
            plan.invalid[path] = (
                f"more than {rules.max_sub_album_levels} folder levels below the album"
            )
            del sizes[path]
    for path, size in sizes.items():
        plan.media.append(path)
        plan.size += size
        folders = below(path, plan.prefix)[:-1]
        for depth in range(1, len(folders) + 1):
            plan.sub_albums.add(folders[:depth])
    if not plan.media and not plan.invalid:
        plan.problems.append("no photos or videos in it")
    return plan


def plan_album(
    folder_name: str, entries: list[Any], album_path: str, rules: Rules
) -> AlbumPlan:
    """The plan for a Dropbox folder, from its listing — before anything is downloaded."""
    prefix_length = len(album_path.rstrip("/")) + 1
    files = [
        (PurePosixPath(entry.path_display[prefix_length:]).parts, entry.size)
        for entry in entries
        if isinstance(entry, dropbox.files.FileMetadata)
    ]
    return plan_files(folder_name, files, rules)


def report_plan(plan: AlbumPlan, *, invalid_dir: Path) -> None:
    sub_albums = f", {len(plan.sub_albums)} sub-album(s)" if plan.sub_albums else ""
    out.info(f"  {plan.files} file(s), {human_size(plan.size)}{sub_albums}")
    report_lines(
        "metadata (sent; the server skips it)",
        [f"{shown(path)} ({why})" for path, why in plan.metadata.items()],
    )
    if plan.invalid:
        out.warn(
            f"  {len(plan.invalid)} file(s) the server would refuse — kept out of the upload,"
            f" in {invalid_dir}:"
        )
        for path, why in plan.invalid.items():
            out.warn(f"      {shown(path)}: {why}")


def report_lines(label: str, lines: list[str], limit: int = 10) -> None:
    for line in lines[:limit]:
        out.info(f"    {label}: {line}")
    if len(lines) > limit:
        out.info(f"    … and {len(lines) - limit} more {label.split(' ')[0]} file(s)")


# --- The ZIP on its way ---------------------------------------------------------------------------


def member_name(member: zipfile.ZipInfo) -> str:
    """A member's name as the server reads it (photo_album.services._member_name)."""
    name = member.filename
    if not member.flag_bits & 0x800:
        try:
            name = name.encode("cp437").decode("utf-8")
        except UnicodeError:
            pass
    return unicodedata.normalize("NFC", name.replace("\\", "/"))


def zip_time(stamp: float) -> tuple[int, int, int, int, int, int]:
    """A file's modification time as a ZIP entry holds it. The server reads it as the photo's."""
    moment = time.localtime(stamp)
    # 1980 is as early as ZIP goes; the server reads 1980-01-01 as "no time".
    if moment.tm_year < 1980:
        return (1980, 1, 1, 0, 0, 0)
    return (
        moment.tm_year,
        moment.tm_mon,
        moment.tm_mday,
        moment.tm_hour,
        moment.tm_min,
        moment.tm_sec,
    )


@dataclass
class Outgoing:
    """A ZIP to send, and where each of its files is in the Dropbox folder."""

    path: Path
    album: str  # the album it is sent to by name, or "" for the server to name it as it descends
    # Name in the ZIP -> path below the Dropbox folder.
    members: dict[str, tuple[str, ...]]


def dropbox_entries(archive: Path) -> dict[str, tuple[tuple[str, ...], int]]:
    """The files in a ZIP from Dropbox: name -> (path below the folder it wraps them in, size)."""
    with zipfile.ZipFile(archive) as zip_file:
        members = [
            (member_name(info), info.file_size)
            for info in zip_file.infolist()
            if not info.is_dir()
        ]
    entries: dict[str, tuple[tuple[str, ...], int]] = {}
    for name, size in members:
        parts = PurePosixPath(name).parts
        if len(parts) < 2 or any(part in {"/", ".", ".."} for part in parts):
            raise AlbumFailed(
                f"The ZIP from Dropbox holds {name!r}, which is not inside its one folder."
            )
        entries[name] = (parts[1:], size)
    return entries


def save_members(
    outgoing: Outgoing, names: list[str], target: Path, *, label: str
) -> None:
    """Copy these members out of the ZIP to `target`, at their paths in the Dropbox folder.

    A file already there is left alone: it may be one fixed since, or the very file being retried.
    """
    saved = 0
    try:
        with zipfile.ZipFile(outgoing.path) as zip_file:
            by_name = {member_name(info): info for info in zip_file.infolist()}
            for name in names:
                info = by_name[name]
                destination = target.joinpath(*outgoing.members[name])
                if destination.exists():
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with zip_file.open(info) as source, destination.open("wb") as copy:
                    shutil.copyfileobj(source, copy, CHUNK_SIZE)
                saved += 1
                # Kept, so a retry sends the time Dropbox had — the server reads it as the photo's.
                stamp = time.mktime((*info.date_time, 0, 0, -1))
                os.utime(destination, (stamp, stamp))
    except OSError as exc:
        raise Fatal(f"Cannot write to {target}: {exc}") from exc
    if saved:
        out.info(f"  {saved} {label} file(s) saved in {target}")


def without(outgoing: Outgoing, drop: set[str], plan: AlbumPlan) -> Outgoing:
    """The ZIP without `drop`, its paths made relative to the album and sent to it by name.

    By name, because without those files the server might descend further than it would have —
    and name the album after a sub-album.
    """
    partial = outgoing.path.with_name(outgoing.path.name + ".partial")
    members: dict[str, tuple[str, ...]] = {}
    try:
        with (
            zipfile.ZipFile(outgoing.path) as source,
            zipfile.ZipFile(partial, "w") as target,
        ):
            for info in source.infolist():
                name = member_name(info)
                if name in drop or name not in outgoing.members:
                    continue
                path = outgoing.members[name]
                new_name = shown(below(path, plan.prefix))
                if new_name in members:
                    continue  # metadata outside the album that meets its namesake inside; saved anyway
                new_info = zipfile.ZipInfo(new_name, date_time=info.date_time)
                with (
                    source.open(info) as data,
                    target.open(new_info, "w", force_zip64=True) as copy,
                ):
                    shutil.copyfileobj(data, copy, CHUNK_SIZE)
                members[new_name] = path
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise Fatal(f"Cannot write {partial}: {exc}") from exc
    partial.replace(outgoing.path)
    return Outgoing(outgoing.path, plan.name, members)


def refused_members(errors: list[str], members: dict[str, Any]) -> list[str]:
    """The members the server's reasons name — if every reason names one, otherwise none.

    The server writes "<member>: <why>" for a file, and something else for the album as a whole
    (a folder name it cannot use, a locked album); only the first can be fixed by leaving files out.
    """
    refused: set[str] = set()
    for error in errors:
        named = [name for name in members if error.startswith(f"{name}: ")]
        if not named:
            return []
        refused.add(max(named, key=len))
    return sorted(refused)


def waiting(album_dir: Path) -> list[Path]:
    """The files in an album's invalid/ folder, not counting what Finder leaves there itself."""
    invalid_dir = album_dir / "invalid"
    if not invalid_dir.is_dir():
        return []
    return sorted(
        path
        for path in invalid_dir.rglob("*")
        if path.is_file() and path.name != ".DS_Store"
    )


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
    album_dir = options.work_dir / year / folder_name
    previous = state.get(album_path)
    if previous.get("status") == "imported":
        out.ok(
            f'  ✓ Imported on an earlier run as "{year}/{previous.get("album") or folder_name}".'
        )
        return "done"
    if previous.get("status") == "submitted":
        out.info("  Sent on an earlier run; asking the server how it went …")
        job = wait_for_job(server, options, previous["statusUrl"])
        return imported(state, album_path, previous.get("album"), job, album_dir)
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
            f"{len(plan.problems)} problem(s) in Dropbox:",
            plan.problems,
        )
    if same_name(plan.name) in existing:
        out.ok(f'  ✓ Already on the server as "{year}/{plan.name}" — skipped.')
        return "exists"

    if plan.name != folder_name:
        out.info(f'  As album "{plan.name}"')
    report_plan(plan, invalid_dir=album_dir / "invalid")
    if options.dry_run:
        return "would import" if plan.media else "failed"
    if options.confirm and not ask(f"  Import {year}/{folder_name}?"):
        out.info("  Skipped (you said no).")
        return "declined"

    target_dir = options.work_dir / year
    target_dir.mkdir(parents=True, exist_ok=True)
    archive = target_dir / f"{plan.name.replace('/', '_')}.zip"
    out.info("  Downloading from Dropbox …")
    download_zip(client, album_path, archive, plan.total_files)
    try:
        # From here on, what the ZIP holds is what counts — Dropbox's listing was the forecast.
        entries_in_zip = dropbox_entries(archive)
        plan = plan_files(folder_name, list(entries_in_zip.values()), rules)
        outgoing = Outgoing(
            archive, "", {name: path for name, (path, _) in entries_in_zip.items()}
        )
        names = {path: name for name, path in outgoing.members.items()}
        write_manifest(album_dir, year=year, album_path=album_path, plan=plan)
        save_members(
            outgoing,
            [names[path] for path in plan.metadata],
            album_dir / "metadata",
            label="metadata",
        )
        save_members(
            outgoing,
            [names[path] for path in plan.invalid],
            album_dir / "invalid",
            label="kept-out",
        )
        if not plan.media:
            raise AlbumFailed(
                f"Nothing in it the server accepts. The {len(plan.invalid)} file(s) are in"
                f" {album_dir / 'invalid'}; fix them there and send them with --retry-invalid."
            )
        if plan.invalid:
            outgoing = without(outgoing, {names[path] for path in plan.invalid}, plan)

        def submitted(status_url: str) -> None:
            state.set(
                album_path, status="submitted", album=plan.name, statusUrl=status_url
            )

        job, _ = send(
            server,
            options,
            outgoing,
            plan,
            year=year,
            album_dir=album_dir,
            submitted=submitted,
        )
        return imported(state, album_path, plan.name, job, album_dir)
    finally:
        archive.unlink(
            missing_ok=True
        )  # Dropbox still has it; the disk should not fill up with retries


def write_manifest(
    album_dir: Path, *, year: str, album_path: str, plan: AlbumPlan
) -> None:
    """What --retry-invalid needs to send this folder's kept-out files to the right album."""
    album_dir.mkdir(parents=True, exist_ok=True)
    (album_dir / "album.json").write_text(
        json.dumps(
            {
                "year": year,
                "dropboxPath": album_path,
                "album": plan.name,
                "prefix": list(plan.prefix),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


MAX_RESENDS = 3


def send(
    server: Server,
    options: Options,
    outgoing: Outgoing,
    plan: AlbumPlan,
    *,
    year: str,
    album_dir: Path,
    submitted: Callable[[str], None],
) -> tuple[dict[str, Any], Outgoing]:
    """Send the ZIP and wait for the server's result; returns it, and what was sent in the end.

    Files the server refuses one by one — content it cannot read, which no listing shows — are
    moved to invalid/ and the rest sent again. An import is all or nothing, so nothing of the
    refused attempt was stored.
    """
    for attempt in range(MAX_RESENDS + 1):
        out.info(
            f"  Sending {human_size(outgoing.path.stat().st_size)} to the server …"
        )
        status_url = server.submit(outgoing.path, year, album=outgoing.album)
        submitted(status_url)
        try:
            return wait_for_job(server, options, status_url), outgoing
        except AlbumFailed as failure:
            refused = refused_members(failure.details, outgoing.members)
            if not refused or attempt == MAX_RESENDS:
                raise
            media = set(plan.media)
            if all(
                path not in media or name in refused
                for name, path in outgoing.members.items()
            ):
                save_members(outgoing, refused, album_dir / "invalid", label="kept-out")
                raise AlbumFailed(
                    "The server refused every photo and video in it; they are in"
                    f" {album_dir / 'invalid'}.",
                    failure.details,
                ) from failure
            out.warn(
                f"  The server refused {len(refused)} file(s); keeping them out and sending the rest again:"
            )
            for line in failure.details:
                out.warn(f"      {line}")
            save_members(outgoing, refused, album_dir / "invalid", label="kept-out")
            outgoing = without(outgoing, set(refused), plan)
    raise AssertionError("unreachable")


PROGRESS_STEPS = {"checking": "checked", "storing": "stored"}


def describe_progress(progress: dict[str, Any] | None) -> str:
    """The server's progress as ' — stored 120 of 400 files', or '' while it reports none."""
    if not progress or progress.get("step") not in PROGRESS_STEPS:
        return ""
    return f" — {PROGRESS_STEPS[progress['step']]} {progress['done']} of {progress['total']} files"


def wait_for_job(server: Server, options: Options, status_url: str) -> dict[str, Any]:
    """Wait for the server to finish an import; returns its result, or raises if it was refused."""
    started = time.monotonic()
    last_state = None
    last_report = started
    while True:
        job = server.status(status_url)
        if job["state"] != last_state:
            out.info(f"  Server: {job['state']}")
            last_state = job["state"]
        elif time.monotonic() - last_report > 60:
            out.info(
                f"    … still {job['state']} ({int(time.monotonic() - started) // 60} min)"
                f"{describe_progress(job.get('progress'))}"
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
            return job
        if job["state"] == "failed":
            raise AlbumFailed(
                f"The server refused the album: {job.get('error') or 'no reason given'}",
                job.get("errors") or [],
            )
        time.sleep(options.poll_interval)


def imported(
    state: State,
    album_path: str,
    album: str | None,
    job: dict[str, Any],
    album_dir: Path,
) -> str:
    kept_out = len(waiting(album_dir))
    state.set(
        album_path,
        status="imported",
        album=album,
        albums=[entry["path"] for entry in job.get("albums", [])],
        skipped=job.get("skipped", []),
        keptOut=kept_out,
    )
    report_job(job, album)
    if kept_out:
        out.warn(
            f"  {kept_out} file(s) were kept out and wait in {album_dir / 'invalid'}."
        )
    return "imported"


def report_job(job: dict[str, Any], album: str | None) -> None:
    albums = job.get("albums", [])
    verb = "Added to the existing album" if job.get("merged") else "Imported"
    out.ok(
        f"  ✓ {verb}{' and locked' if job.get('locked') else ''}:"
        f" {albums[0]['path'] if albums else album}"
        + (f" (+{len(albums) - 1} sub-album(s))" if len(albums) > 1 else "")
    )
    metadata = 0
    for line in job.get("skipped", []):
        if line.endswith(METADATA_SKIPPED):
            metadata += 1  # .DS_Store, ._*, Thumbs.db, …: there are a lot, and they are kept in metadata/
        else:
            out.info(f"    left out: {line}")
    if metadata:
        out.info(f"    left out: {metadata} metadata file(s)")


# --- Retrying the kept-out files of one album -----------------------------------------------------


def retry_invalid(server: Server, options: Options, album_dir: Path) -> int:
    """Send the files waiting in an album's invalid/ folder to that album, which they are merged into.

    Run it once they are fixed: converted, shrunk, or replaced (the folder mirrors the Dropbox one).
    What is still invalid stays where it is; what the server took moves on to retried/.
    """
    try:
        manifest = json.loads((album_dir / "album.json").read_text(encoding="utf-8"))
        year, album, prefix = (
            manifest["year"],
            manifest["album"],
            tuple(manifest["prefix"]),
        )
    except (OSError, ValueError, KeyError) as exc:
        raise Fatal(
            f"{album_dir} is not an album folder this script wrote (no readable album.json: {exc})."
        ) from exc
    invalid_dir = album_dir / "invalid"
    files = {path.relative_to(invalid_dir).parts: path for path in waiting(album_dir)}
    out.heading(f"{year}/{album} ← {invalid_dir}")
    if not files:
        out.ok(f"  Nothing waiting in {invalid_dir}.")
        out.info(
            "  An album the server refused as a whole is sent again from Dropbox with"
            f" --retry-failed --year {year}."
        )
        return 0

    existing, rules = server.albums(year)
    plan = plan_files(
        album_dir.name,
        [(path, file.stat().st_size) for path, file in files.items()],
        rules,
        pinned=(album, prefix),
    )
    out.info(f"  {plan.files} file(s) to send, {human_size(plan.size)}")
    report_lines(
        "metadata (not sent)",
        [f"{shown(path)} ({why})" for path, why in plan.metadata.items()],
    )
    for path, why in plan.invalid.items():
        out.warn(f"    still refused, stays: {shown(path)}: {why}")
    if not plan.media:
        out.warn("  Nothing the server would accept yet.")
        return 1 if plan.invalid else 0
    if same_name(album) not in existing:
        out.warn(
            f'  "{year}/{album}" is not on the server; it will be created with these files.'
        )
    if options.dry_run:
        return 0

    archive = album_dir.parent / f"{album_dir.name}.retry.zip"
    partial = archive.with_name(archive.name + ".partial")
    members: dict[str, tuple[str, ...]] = {}
    try:
        with zipfile.ZipFile(partial, "w") as zip_file:
            for path in plan.media:
                name = shown(below(path, prefix))
                info = zipfile.ZipInfo(
                    name, date_time=zip_time(files[path].stat().st_mtime)
                )
                with (
                    files[path].open("rb") as data,
                    zip_file.open(info, "w", force_zip64=True) as copy,
                ):
                    shutil.copyfileobj(data, copy, CHUNK_SIZE)
                members[name] = path
        partial.replace(archive)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise Fatal(f"Cannot write {partial}: {exc}") from exc
    try:
        job, sent = send(
            server,
            options,
            Outgoing(archive, album, members),
            plan,
            year=year,
            album_dir=album_dir,
            submitted=lambda _url: None,
        )
    finally:
        archive.unlink(missing_ok=True)
    report_job(job, album)
    for path in sent.members.values():
        moved = album_dir.joinpath("retried", *path)
        moved.parent.mkdir(parents=True, exist_ok=True)
        files[path].replace(moved)
    for folder in sorted(invalid_dir.rglob("*"), reverse=True):
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    out.info(f"  {len(sent.members)} file(s) moved to {album_dir / 'retried'}")
    if left := waiting(album_dir):
        out.warn(f"  {len(left)} file(s) still wait in {invalid_dir}.")
    return 0


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
    parser.add_argument(
        "--retry-invalid",
        type=Path,
        metavar="ALBUM_DIR",
        help="send the fixed files in <work-dir>/<year>/<album>/invalid to their album, and nothing else",
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

    if args.retry_invalid:
        album_dir = args.retry_invalid
        if album_dir.name == "invalid":
            album_dir = album_dir.parent
        args.work_dir = album_dir.parent.parent  # its logs are that work directory's
    args.work_dir.mkdir(parents=True, exist_ok=True)
    out.open_log(args.work_dir / "import.log", args.work_dir / "failures.log")
    # A ZIP the last run did not finish. Only beside the ZIPs: the album folders hold Dropbox files.
    for leftover in args.work_dir.glob("*/*.partial"):
        leftover.unlink()
    options = Options(
        args.work_dir, args.poll_interval, args.dry_run, args.confirm, args.retry_failed
    )
    server = Server(args.endpoint, token)
    if args.retry_invalid:
        try:
            return retry_invalid(server, options, album_dir)
        except KeyboardInterrupt:
            out.error("\n✗ Stopped. Run the same command again to send them.")
            return 130
        except (AlbumFailed, requests.RequestException, ValueError) as error:
            failure = (
                error
                if isinstance(error, AlbumFailed)
                else AlbumFailed(f"Unexpected answer: {error}")
            )
            out.failure(f"{album_dir} (retry)", failure.summary, failure.details)
            return 1
        except Fatal as exc:
            out.failure(f"{album_dir} (retry)", str(exc), [])
            return 2
    state = State(args.work_dir / "state.json")
    client = dropbox_client()
    results: dict[str, list[str]] = {}
    kept_out: list[tuple[Path, int]] = []

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
                    if options.dry_run:
                        out.error(f"  ✗ WOULD FAIL: {failure.summary}")
                        for line in failure.details:
                            out.error(f"      {line}")
                    else:
                        out.failure(album_path, failure.summary, failure.details)
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
                    if count := state.get(album_path).get("keptOut"):
                        kept_out.append((args.work_dir / year / entry.name, count))
                results.setdefault(result, []).append(f"{year}/{entry.name}")
    except KeyboardInterrupt:
        out.error(
            "\n✗ Stopped. Progress is saved; run the same command again to continue."
        )
        return 130
    except (Fatal, AlbumFailed, requests.RequestException) as exc:
        out.failure("the run stopped", str(exc), [], echo=False)
        out.error(f"\n✗ {exc}")
        out.error("  Progress is saved; run the same command again once this is fixed.")
        return 2
    finally:
        for leftover in args.work_dir.glob("*/*.partial"):
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
    if kept_out:
        out.warn(
            f"\n{sum(count for _, count in kept_out)} file(s) in {len(kept_out)} imported album(s)"
            " were kept out of the upload. They wait in each album's invalid/ folder:"
        )
        for album_dir, count in kept_out:
            out.warn(f"    {album_dir}/invalid ({count})")
        out.warn(
            "  Fix them there, then send them to their album (unlock it on the site first):\n"
            f"    uv run scripts/import_dropbox_photo_albums.py --retry-invalid {kept_out[0][0]}"
        )
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
                f"  Details are above and in {args.work_dir / 'failures.log'}. Fix them in Dropbox,"
                " then rerun with --retry-failed."
            )
        return 1
    out.ok("\n✓ Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
