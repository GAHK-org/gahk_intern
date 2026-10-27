#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "dropbox>=12.0",
#     "requests>=2.32",
# ]
# ///
"""Import the photo archive in Dropbox (/GAHK billeder/<year>/<album>) into the photo album.

A folder named "Uden årstal" beside the years is imported in the same way, into the server's folder
of that name for albums without a year (--year "Uden årstal" to import only it).

One album at a time: list it in Dropbox, check it against the server's own import rules, download it
as a ZIP, send it straight to the server's bucket in parts and start the import, wait for the
result, delete the ZIP. Sub-folders
become sub-albums (up to the server's depth limit). The server imports an archive completely or
not at all, so a failed album leaves nothing half-imported behind, and locks every album it imports
this way (Fotogruppen can unlock one that needs a change).

Nothing the server took is kept on this disk, and everything it did not take is — so every file in
Dropbox that is not in the photo album can be gone through by hand. <work-dir>/NOT-ON-SERVER.md
lists each one after every run: why, and a link to its copy (not-on-server.json is the same list
for the script). Delete a copy once you have dealt with it; it is then listed apart and not fetched
again. A file moved out of the archive to /til_intern_drev (by move_dropbox_non_media.py, or by
hand) is struck off by the next run that finds it there, and its copy deleted. A file deleted from
Dropbox stays listed, as its copy may be the last. What is kept:

- Files the server would refuse — anything but photos (camera RAW files included) and videos it
  accepts, photos over its size limits (videos have none for this script), files too deep — are
  kept out of the ZIP, so the rest of the album gets in, and saved to
  <work-dir>/<year>/<album>/invalid/ (mirroring the Dropbox folder) to be looked at and fixed. So
  is a file the server refuses on reading it, after which the rest is sent again.
- Metadata (.DS_Store, AppleDouble sidecars, Thumbs.db, …) is sent with the album. The server keeps
  each photo's sidecar with it; a copy of the rest — what it reports skipping — is saved in
  <work-dir>/<year>/<album>/metadata/. A duplicate it skips needs no copy: the album has the file.
- Files beside the album folders in a year folder, and beside the year folders in the Dropbox root,
  are saved in <work-dir>/<year>/_not-in-an-album/ and <work-dir>/_not-in-a-year-folder/.
- Of an album that fails, what is not in its invalid/ stays in <work-dir>/<year>/<album>/failed.zip
  until it is imported.

Once the kept-out files are fixed, --retry-invalid sends them — and only them, with their sidecars —
to their album. The server adds them to it rather than refusing an album that already exists,
storing only files it does not have (the same name with other content is a different photo and is
kept). The album has to be unlocked on the site first. What the server took is deleted; what is
still invalid stays.

--rename-albums goes through the imported albums the server named after a folder inside their
Dropbox folder — it descends through every level holding one folder and no files, so
"2014/GAHK Color Run", holding only "101GOPRO/" (and metadata), became an album "101GOPRO". For each
it offers: skip (ask again next time), keep the leaf folder's name, use the parent folder's name,
join the folder names with a space, or a name of your own; and renames the album on the server,
locked or not. It needs no Dropbox access, and remembers every answer but "skip".

--send-dates sends the AppleDouble sidecars saved in each imported album's metadata/ folder (the old
file server's record of every file's own dates) to the server, which keeps each with its photo and
dates the album's photos by them, as an import now does itself. For albums imported before the
server read sidecars; the copies it kept are deleted here. It needs no Dropbox access, works on
locked albums, and is safe to run again.

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
    uv run scripts/import_dropbox_photo_albums.py --send-dates --year-range 2001-2006
    uv run scripts/import_dropbox_photo_albums.py --rename-albums --year 2014   # --dry-run lists them
    uv run scripts/import_dropbox_photo_albums.py --overview   # NOT-ON-SERVER.md again, after a clean-up

Resuming: progress is saved in <work-dir>/state.json after every step, so stopping (Ctrl-C, a lost
connection, a reboot) is safe — run the same command again and it continues where it stopped. An
album already on the server is skipped; one the server was still importing is waited for rather
than sent twice. A server that stops answering is tried again every 30 seconds, 5 times; then the
run stops, as every album after it would fail too. Albums that failed are skipped on later runs until you fix them in Dropbox and pass
--retry-failed. Everything printed is also appended to <work-dir>/import.log, and every failure —
with all its reasons — to <work-dir>/failures.log.
"""

import argparse
import io
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import zipfile
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, TextIO, TypeVar
from urllib.parse import quote, urljoin

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
# The server's one folder that is not a year (photo_album.models.NO_YEAR_FOLDER): a Dropbox folder of
# that name beside the years is imported into it. Compared in NFC, as Dropbox may decompose the "å".
OTHER_FOLDER = "Uden årstal"
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

    def __init__(
        self,
        summary: str,
        details: list[str] | None = None,
        *,
        kept: Path | None = None,
    ) -> None:
        super().__init__(summary)
        self.summary = summary
        self.details = details or []
        # Where the album's files are kept here, if they were downloaded.
        self.kept = kept


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
        self._write()

    def update(self, key: str, **changes: object) -> None:
        """Change some fields of an entry, keeping the rest — its time included."""
        self.albums[key] = {**self.albums[key], **changes}
        self._write()

    def drop(self, key: str) -> None:
        if self.albums.pop(key, None) is not None:
            self._write()

    def _write(self) -> None:
        partial = self.path.with_suffix(".json.partial")
        partial.write_text(
            json.dumps({"albums": self.albums}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        partial.replace(
            self.path
        )  # atomic: a crash mid-write never leaves a broken state file


# --- What is not on the server ------------------------------------------------------------------

LEDGER = "not-on-server.json"
OVERVIEW = "NOT-ON-SERVER.md"
# Where the files outside the album folders are kept: those beside the albums in a year folder in
# <work-dir>/<year>/NOT_IN_AN_ALBUM, those beside the years in the Dropbox root in <work-dir>/NOT_IN_A_YEAR.
NOT_IN_AN_ALBUM = "_not-in-an-album"
NOT_IN_A_YEAR = "_not-in-a-year-folder"
# Where move_dropbox_non_media.py moves what is not photos, videos or metadata, at the same path below
# it as in the archive. What has moved there is struck off the list.
INTERN_DREV = "/til_intern_drev"
FAILED_ZIP = "failed.zip"  # <work-dir>/<year>/<album>/: what of a failed album is not in invalid/
DOWNLOAD = "download.partial"  # <work-dir>/: a single file on its way from Dropbox
# Why a file is not on the server, in the order the overview lists them.
FILE_KINDS = {
    "kept-out": "Kept out of the upload: the server would refuse them",
    "refused": "Refused by the server when it read them",
    "outside": "Outside the album folders",
    "metadata": "Metadata the server did not keep",
    "duplicate": "Duplicates the server skipped: the album has the same file",
}
ALBUM_KINDS = {
    "failed": "Album folders that failed",
    "exists": "Album folders not sent: an album of that name was on the server already",
}


def same_path(path: str) -> str:
    """Dropbox paths as the list keys them: Dropbox may give a name decomposed, a ZIP composed."""
    return unicodedata.normalize("NFC", path)


class Ledger:
    """Every file in Dropbox that is not on the server, keyed by its Dropbox path: why, and where its
    copy is in the work directory — none for a duplicate, whose content the album has. Also every
    album folder that did not get onto the server.

    Written out as NOT-ON-SERVER.md after every run, for a person to go through. A copy deleted
    there is one dealt with: it is listed apart, and not fetched again. A file the server takes
    later (--retry-invalid, --send-dates, --retry-failed) is struck off, and its copy deleted; so is
    one a run finds moved out of the archive to INTERN_DREV (move_dropbox_non_media.py).
    """

    def __init__(self, work_dir: Path, *, read_only: bool = False) -> None:
        self.work_dir = work_dir
        self.path = work_dir / LEDGER
        self.read_only = read_only  # a dry run changes nothing
        self.files: dict[str, dict[str, Any]] = {}
        self.albums: dict[str, dict[str, Any]] = {}
        self.new = not self.path.exists()
        if not self.new:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.files, self.albums = data["files"], data["albums"]
            except (ValueError, KeyError) as exc:
                die(
                    f"{self.path} is not a list this script wrote ({exc}). Move it away to start it over."
                )

    def get(self, dropbox_path: str) -> dict[str, Any] | None:
        return self.files.get(same_path(dropbox_path))

    def _key(self, dropbox_path: str) -> str | None:
        """The list's key for a Dropbox path, which ignores case as Dropbox does."""
        key = same_path(dropbox_path)
        if key in self.files:
            return key
        return next((each for each in self.files if each.lower() == key.lower()), None)

    def copy(self, entry: dict[str, Any]) -> Path | None:
        return self.work_dir / entry["copy"] if entry.get("copy") else None

    def dealt_with(self, entry: dict[str, Any]) -> bool:
        copy = self.copy(entry)
        return copy is not None and not copy.exists()

    def file(
        self, dropbox_path: str, kind: str, why: str, *, copy: Path | None, size: int
    ) -> None:
        self.files[same_path(dropbox_path)] = {
            "kind": kind,
            "why": why,
            "copy": copy.relative_to(self.work_dir).as_posix() if copy else None,
            "size": size,
        }

    def on_server(self, dropbox_path: str) -> None:
        """The server has it now: strike it off, and delete the copy."""
        self._strike(dropbox_path)

    def moved_out(self, dropbox_path: str) -> None:
        """It is no longer in the archive but in INTERN_DREV: strike it off, and delete the copy,
        which Dropbox does not need."""
        self._strike(dropbox_path)

    def _strike(self, dropbox_path: str) -> None:
        key = self._key(dropbox_path)
        entry = self.files.pop(key) if key is not None else None
        if entry and (copy := self.copy(entry)) and not self.read_only:
            copy.unlink(missing_ok=True)

    def album(
        self,
        dropbox_path: str,
        kind: str,
        why: str,
        *,
        details: list[str] | None = None,
        copy: Path | None = None,
    ) -> None:
        self.albums[same_path(dropbox_path)] = {
            "kind": kind,
            "why": why,
            "details": details or [],
            "copy": copy.relative_to(self.work_dir).as_posix() if copy else None,
        }

    def album_imported(self, dropbox_path: str) -> None:
        self.albums.pop(same_path(dropbox_path), None)

    def save(self) -> None:
        if self.read_only:
            return
        partial = self.path.with_suffix(".json.partial")
        partial.write_text(
            json.dumps(
                {
                    "files": dict(sorted(self.files.items())),
                    "albums": dict(sorted(self.albums.items())),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        partial.replace(self.path)


def cell(text: str) -> str:
    """Text for a Markdown table cell: nothing in it read as markup, control characters shown."""
    text = "".join(
        repr(char)[1:-1] if unicodedata.category(char) == "Cc" else char
        for char in text
    )
    return re.sub(r"([\\`*_\[\]<>|#])", r"\\\1", text)


def reasons(entry: dict[str, Any]) -> str:
    """An album's reasons on one line."""
    return "; ".join(line.rstrip(".") for line in [entry["why"], *entry["details"]])


def link(copy: str | None) -> str:
    return f"[{cell(copy)}]({quote(copy)})" if copy else "—"


def write_overview(ledger: Ledger) -> Path:
    """NOT-ON-SERVER.md: the list, for a person to go through."""
    pending = {
        path: entry
        for path, entry in ledger.files.items()
        if not ledger.dealt_with(entry)
    }
    dealt = {
        path: entry for path, entry in ledger.files.items() if ledger.dealt_with(entry)
    }
    lines = [
        "# Not on the server",
        "",
        (
            "Every file in Dropbox that is not in the photo album, and why, as"
            " scripts/import_dropbox_photo_albums.py last found it"
            f" ({datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC). Its copy is in this folder;"
            " the link opens it. Decide what to do with each: fix it and send it with"
            " --retry-invalid, add it on the site, or let it go — then delete the copy. A deleted"
            " copy is listed as dealt with at the end, and is not fetched again."
        ),
        "",
        "| | Count | Size |",
        "|---|---:|---:|",
    ]
    for kind, label in ALBUM_KINDS.items():
        if count := sum(1 for entry in ledger.albums.values() if entry["kind"] == kind):
            lines.append(f"| {label} | {count} | |")
    for kind, label in FILE_KINDS.items():
        entries = [entry for entry in pending.values() if entry["kind"] == kind]
        if entries:
            size = human_size(sum(entry["size"] for entry in entries))
            lines.append(f"| {label} | {len(entries)} | {size} |")
    if dealt:
        lines.append(f"| Dealt with: the copy is deleted | {len(dealt)} | |")
    if not ledger.albums and not ledger.files:
        lines.append("| Nothing: every file is on the server | 0 | |")
    for kind, label in ALBUM_KINDS.items():
        rows = [
            f"| {cell(path)} | {cell(reasons(entry))} | {link(entry['copy'])} |"
            for path, entry in sorted(ledger.albums.items())
            if entry["kind"] == kind
        ]
        if rows:
            lines += [
                "",
                f"## {label} ({len(rows)})",
                "",
                "| Album folder in Dropbox | Why | Copy here |",
                "|---|---|---|",
                *rows,
            ]
    for kind, label in FILE_KINDS.items():
        rows = [
            f"| {cell(path)} | {human_size(entry['size'])} | {cell(entry['why'])}"
            f" | {link(entry['copy'])} |"
            for path, entry in sorted(pending.items())
            if entry["kind"] == kind
        ]
        if rows:
            lines += [
                "",
                f"## {label} ({len(rows)})",
                "",
                "| File in Dropbox | Size | Why | Copy here |",
                "|---|---:|---|---|",
                *rows,
            ]
    if dealt:
        lines += [
            "",
            f"## Dealt with: the copy is deleted ({len(dealt)})",
            "",
            "| File in Dropbox | Why it is not on the server |",
            "|---|---|",
            *(
                f"| {cell(path)} | {cell(entry['why'])} |"
                for path, entry in sorted(dealt.items())
            ),
        ]
    path = ledger.work_dir / OVERVIEW
    if not ledger.read_only:
        partial = path.with_name(path.name + ".partial")
        partial.write_text("\n".join(lines) + "\n", encoding="utf-8")
        partial.replace(path)
    return path


def report_not_on_server(ledger: Ledger) -> None:
    if ledger.read_only and ledger.new:
        return  # a dry run keeps no copies, and no list of them
    overview = write_overview(ledger)
    pending = [entry for entry in ledger.files.values() if not ledger.dealt_with(entry)]
    out.heading("Not on the server")
    for kind, label in ALBUM_KINDS.items():
        if count := sum(1 for entry in ledger.albums.values() if entry["kind"] == kind):
            out.info(f"  {count:4d}  {label.lower()}")
    for kind, label in FILE_KINDS.items():
        if count := sum(1 for entry in pending if entry["kind"] == kind):
            out.info(f"  {count:4d}  {label.lower()}")
    if dealt := len(ledger.files) - len(pending):
        out.info(f"  {dealt:4d}  dealt with: the copy is deleted")
    if not ledger.albums and not ledger.files:
        out.ok("  Nothing: every file is on the server.")
    elif ledger.read_only:
        out.info(
            f"  As the last run left them in {overview} (a dry run writes nothing)."
        )
    else:
        out.info(f"  Each one, why, and where its copy is: {overview}")


def prune(folder: Path) -> None:
    """Remove the folders left empty below `folder`, and it too if it is."""
    if not folder.is_dir():
        return
    for each in sorted(folder.rglob("*"), reverse=True):
        if each.is_dir() and not any(each.iterdir()):
            each.rmdir()
    if not any(folder.iterdir()):
        folder.rmdir()


# --- The server ---------------------------------------------------------------------------------


@dataclass
class Rules:
    """What the server accepts in an archive, as it reports it (photo_album.views.system_albums)."""

    image_extensions: frozenset[str]
    video_extensions: frozenset[str]
    image_max_bytes: int
    # None: no limit — the server takes this script's videos whatever their size.
    video_max_bytes: int | None
    max_sub_album_levels: int
    macos_metadata_max_bytes: int
    macos_metadata_any_size: frozenset[str]
    # Camera RAW files (also among the image extensions), which have a limit of their own.
    raw_extensions: frozenset[str] = frozenset()
    raw_max_bytes: int = 0

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Rules":
        video_max_mb = data["videoMaxMb"]
        return cls(
            image_extensions=frozenset(data["imageExtensions"]),
            video_extensions=frozenset(data["videoExtensions"]),
            image_max_bytes=int(data["imageMaxMb"]) * 1024 * 1024,
            video_max_bytes=None
            if video_max_mb is None
            else int(video_max_mb) * 1024 * 1024,
            max_sub_album_levels=int(data["maxSubAlbumLevels"]),
            macos_metadata_max_bytes=int(data["macosMetadataMaxBytes"]),
            # Absent from servers older than the .DS_Store exception, which had no such names.
            macos_metadata_any_size=frozenset(data.get("macosMetadataAnySize", [])),
            # Absent from servers older than RAW files, which took none.
            raw_extensions=frozenset(data.get("rawExtensions", [])),
            raw_max_bytes=int(data.get("rawMaxMb", 0)) * 1024 * 1024,
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
        # For the presigned part URLs (photo_album.direct_upload), which must not carry the token.
        self.bucket = requests.Session()

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

        Straight to the server's bucket, in parts, when it has one (photo_album.direct_upload):
        through the app, the proxy in front of it cuts any upload that takes more than a minute,
        which stopped the 3.2 GB "Bornholm med GAHK - April 06" twelve times. Then the import is
        started with the upload's ticket, a request of a few hundred bytes.

        Without a bucket the ZIP goes with the request, as before, and is sent again if the
        connection fails: the server only takes an import on once the whole upload is in, and
        answers it at once, so a connection lost on the way means it has none.
        """
        upload = self._begin_upload(archive)
        if upload is not None:
            out.info(f"    straight to the bucket, in {len(upload['urls'])} part(s)")
            etags = self._send_parts(archive, upload)
            response = self._request(
                "POST",
                self.endpoint,
                what=f"start the import of {archive.name}",
                data={
                    "folder": folder,
                    "album": album,
                    "ticket": upload["ticket"],
                    "parts": json.dumps(etags),
                },
                timeout=(30, 300),
            )
        else:
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

    def _begin_upload(self, archive: Path) -> dict[str, Any] | None:
        """The bucket upload the server sets up for `archive`: a ticket, the part size and a URL
        per part. None to send the ZIP with the import instead — a server without a bucket says so,
        and one older than direct uploads has no such address."""
        response = self._request(
            "POST",
            f"{self.endpoint}/upload",
            what=f"ask where to send {archive.name}",
            data={"name": archive.name, "size": archive.stat().st_size},
            timeout=60,
        )
        if response.status_code == 404:
            return None
        if response.status_code == 400:
            raise AlbumFailed("The server refused the upload.", form_errors(response))
        response.raise_for_status()
        upload = response.json()
        return upload if upload.get("direct") else None

    def _send_parts(self, archive: Path, upload: dict[str, Any]) -> list[str]:
        """Send `archive` to the upload's part URLs, a part at a time; returns their ETags in order."""
        size, part_size, urls = (
            archive.stat().st_size,
            int(upload["partSize"]),
            upload["urls"],
        )
        etags: list[str] = []
        reported = 0
        with archive.open("rb") as handle:
            for number, url in enumerate(urls, 1):
                handle.seek((number - 1) * part_size)
                etags.append(
                    self._send_part(
                        url, handle.read(part_size), f"part {number} of {len(urls)}"
                    )
                )
                sent = min(number * part_size, size)
                if sent - reported >= size / 10 or number == len(urls):
                    out.info(f"    … {human_size(sent)} of {human_size(size)} sent")
                    reported = sent
        return etags

    def _send_part(self, url: str, data: bytes, what: str) -> str:
        """PUT one part to its presigned URL; returns the bucket's ETag for it.

        Tried again on its own, as `_request` tries a request: a busy bucket RETRIES times before
        the album fails, an unreachable one UNREACHABLE_RETRIES times before the run stops. The URL
        is the credential, so none of the session's headers go with it.
        """
        busy = unreachable = 0
        while True:
            try:
                response = self.bucket.put(url, data=data, timeout=(30, 300))
            except (requests.ConnectionError, requests.Timeout) as exc:
                unreachable += 1
                if unreachable > UNREACHABLE_RETRIES:
                    raise Fatal(
                        f"The bucket did not answer {UNREACHABLE_RETRIES + 1} times in a row while"
                        f" sending {what} ({exc.__class__.__name__}). Giving up."
                    ) from exc
                out.warn(
                    f"    … {what}: no answer ({exc.__class__.__name__}); trying again in"
                    f" {UNREACHABLE_WAIT}s ({unreachable}/{UNREACHABLE_RETRIES})"
                )
                time.sleep(UNREACHABLE_WAIT)
                continue
            if response.ok and response.headers.get("ETag"):
                return response.headers["ETag"]
            if not _transient(response):
                raise AlbumFailed(
                    f"The bucket refused {what}: HTTP {response.status_code}.",
                    [response.text[:500]],
                )
            busy += 1
            if busy == RETRIES:
                raise AlbumFailed(
                    f"Could not send {what}: HTTP {response.status_code}."
                )
            wait = 2**busy
            out.warn(
                f"    … {what}: HTTP {response.status_code}; retrying in {wait}s ({busy}/{RETRIES - 1})"
            )
            time.sleep(wait)

    def rename(self, folder: str, album: str, name: str) -> dict[str, Any]:
        """Rename the root album `album` in `folder` (locked or not); returns it as it is now."""
        response = self._request(
            "POST",
            f"{self.endpoint}/omdoeb",
            what=f"rename {folder}/{album}",
            data={"folder": folder, "album": album, "name": name},
            timeout=60,
        )
        if response.status_code == 404 and "errors" not in response.text:
            raise Fatal(
                f"{self.endpoint}/omdoeb does not exist. Is the server running a version that renames albums?"
            )
        if response.status_code != 200:
            raise AlbumFailed(
                f"The server did not rename {folder}/{album}:", form_errors(response)
            )
        return dict(response.json())

    def send_dates(self, archive: bytes, folder: str, album: str) -> dict[str, Any]:
        """Send an album's sidecars (photo_album.services.apply_sidecars); returns what it did."""
        response = self._request(
            "POST",
            f"{self.endpoint}/datoer",
            what=f"send the dates for {folder}/{album}",
            data={"folder": folder, "album": album},
            files={"archive": ("datoer.zip", archive, "application/zip")},
            timeout=(30, 300),
        )
        if response.status_code == 404:
            raise Fatal(
                f"{self.endpoint}/datoer does not exist. Is the server running a version that reads sidecars?"
            )
        if response.status_code == 400:
            raise AlbumFailed("The server refused the dates.", form_errors(response))
        response.raise_for_status()
        return response.json()

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


def download_file(
    client: dropbox.Dropbox,
    source: str,
    target: Path,
    partial: Path,
    modified: datetime | None,
) -> None:
    """Download one file to `target`, all of it or nothing, with the time Dropbox has for it.

    Written to `partial` first — one place, as the folders it goes to hold Dropbox's own files.
    """
    for attempt in range(1, RETRIES + 1):
        try:
            _, response = with_dropbox_retries(
                f"download {source}", lambda: client.files_download(source)
            )
            with response, partial.open("wb") as destination:
                for chunk in response.iter_content(CHUNK_SIZE):
                    destination.write(chunk)
            break
        except ApiError as exc:
            partial.unlink(missing_ok=True)
            raise AlbumFailed(f"Dropbox refused to send {source}: {exc.error}") from exc
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
            time.sleep(2**attempt)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise Fatal(f"Cannot write {partial}: {exc}") from exc
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial.replace(target)
        if modified is not None:
            stamp = modified.replace(tzinfo=UTC).timestamp()  # Dropbox's times are UTC
            os.utime(target, (stamp, stamp))
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise Fatal(f"Cannot write {target}: {exc}") from exc


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
    limit: int | None
    if extension in rules.raw_extensions:
        limit = rules.raw_max_bytes
    elif extension in rules.image_extensions:
        limit = rules.image_max_bytes
    elif extension in rules.video_extensions:
        limit = rules.video_max_bytes
    else:
        return (
            "invalid",
            f"not a photo or video the album accepts ({extension or 'no extension'})",
        )
    if limit is not None and size > limit:
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
        "metadata (sent with it)",
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
    sizes: dict[tuple[str, ...], int] = field(default_factory=dict)  # by that path


@dataclass
class Folder:
    """One album folder in Dropbox, and its folder here, where what the server lacks of it is kept."""

    ledger: Ledger
    dropbox_path: str
    dir: Path

    def dropbox(self, path: tuple[str, ...]) -> str:
        """The Dropbox path of the file at `path` below the folder."""
        return f"{self.dropbox_path}/{shown(path)}"


# Where an album's copies are kept, by why the server lacks them.
KEPT_IN = {"kept-out": "invalid", "refused": "invalid", "metadata": "metadata"}


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
    outgoing: Outgoing, whys: dict[str, str], folder: Folder, *, kind: str
) -> None:
    """Keep a copy of these members — name -> why the server lacks it — in the album's invalid/ or
    metadata/ folder, at their paths in the Dropbox folder, and list them as `kind`.

    A file already there is left alone: it may be one fixed since, or the very file being retried.
    """
    target = folder.dir / KEPT_IN[kind]
    saved = 0
    try:
        with zipfile.ZipFile(outgoing.path) as zip_file:
            by_name = {member_name(info): info for info in zip_file.infolist()}
            for name, why in whys.items():
                info = by_name[name]
                path = outgoing.members[name]
                destination = target.joinpath(*path)
                folder.ledger.file(
                    folder.dropbox(path),
                    kind,
                    why,
                    copy=destination,
                    size=info.file_size,
                )
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
        out.info(f"  {saved} {kind} file(s) saved in {target}")


def without(
    outgoing: Outgoing, drop: set[str], plan: AlbumPlan, folder: Folder
) -> Outgoing:
    """The ZIP without `drop`, its paths made relative to the album and sent to it by name.

    By name, because without those files the server might descend further than it would have —
    and name the album after a sub-album.
    """
    partial = outgoing.path.with_name(outgoing.path.name + ".partial")
    members: dict[str, tuple[str, ...]] = {}
    left_out: dict[str, str] = {}
    try:
        with (
            zipfile.ZipFile(outgoing.path) as source,
            zipfile.ZipFile(partial, "w") as target,
        ):
            # The album's own files first: one outside it is what gives way to its namesake.
            infos = sorted(
                source.infolist(),
                key=lambda info: (
                    outgoing.members.get(member_name(info), ())[: len(plan.prefix)]
                    != plan.prefix
                ),
            )
            for info in infos:
                name = member_name(info)
                if name in drop or name not in outgoing.members:
                    continue
                path = outgoing.members[name]
                new_name = shown(below(path, plan.prefix))
                # Metadata outside the album, meeting its namesake inside.
                if new_name in members:
                    left_out[name] = (
                        "metadata outside the album the folder makes, with the name of a file in it"
                    )
                    continue
                new_info = zipfile.ZipInfo(new_name, date_time=info.date_time)
                with (
                    source.open(info) as data,
                    target.open(new_info, "w", force_zip64=True) as copy,
                ):
                    shutil.copyfileobj(data, copy, CHUNK_SIZE)
                members[new_name] = path
        if left_out:
            save_members(outgoing, left_out, folder, kind="metadata")
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise Fatal(f"Cannot write {partial}: {exc}") from exc
    partial.replace(outgoing.path)
    return Outgoing(outgoing.path, plan.name, members, outgoing.sizes)


def by_member(
    lines: list[str], members: Collection[str]
) -> tuple[dict[str, str], list[str]]:
    """The server's "<member>: <why>" lines as member -> why, the longest name that fits — and the
    lines that name no member."""
    named: dict[str, str] = {}
    other: list[str] = []
    for line in lines:
        cuts = [match.start() for match in re.finditer(": ", line)]
        name = next(
            (line[:cut] for cut in reversed(cuts) if line[:cut] in members), None
        )
        if name is None:
            other.append(line)
        else:
            named[name] = line[len(name) + 2 :]
    return named, other


def refused_members(errors: list[str], members: Collection[str]) -> dict[str, str]:
    """The members the server's reasons name, and why — if every reason names one, otherwise none.

    The server writes "<member>: <why>" for a file, and something else for the album as a whole
    (a folder name it cannot use, a locked album); only the first can be fixed by leaving files out.
    """
    named, other = by_member(errors, members)
    return {} if other else named


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
    ledger: Ledger,
    options: Options,
    *,
    year: str,
    folder_name: str,
    album_path: str,
    existing: set[str],
    rules: Rules,
) -> str:
    """Take one Dropbox folder as far as it will go. Returns its final status for the summary."""
    folder = Folder(ledger, album_path, options.work_dir / year / folder_name)
    album_dir = folder.dir
    previous = state.get(album_path)
    if previous.get("status") == "imported":
        out.ok(
            f'  ✓ Imported on an earlier run as "{year}/{previous.get("album") or folder_name}".'
        )
        return "done"
    if previous.get("status") == "submitted":
        out.info("  Sent on an earlier run; asking the server how it went …")
        job = wait_for_job(server, options, previous["statusUrl"])
        fetch_what_the_server_left(client, job, previous, folder, options=options)
        return imported(state, album_path, previous.get("album"), job, folder)
    if previous.get("status") == "failed" and not options.retry_failed:
        out.warn(
            "  Failed on an earlier run — skipped. Fix it in Dropbox, then rerun with --retry-failed."
        )
        for line in previous.get("errors", [])[:5]:
            out.warn(f"      {line}")
        return "failed"

    entries = list(list_folder(client, album_path, recursive=True))
    plan = plan_album(folder_name, entries, album_path, rules)
    # A folder of nothing but metadata is downloaded all the same, for a copy of it to be kept.
    if plan.problems and (options.dry_run or not plan.metadata):
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
            archive,
            "",
            {name: path for name, (path, _) in entries_in_zip.items()},
            dict(entries_in_zip.values()),
        )
        names = {path: name for name, path in outgoing.members.items()}
        write_manifest(album_dir, year=year, album_path=album_path, plan=plan)
        save_members(
            outgoing,
            {names[path]: why for path, why in plan.invalid.items()},
            folder,
            kind="kept-out",
        )
        if not plan.media:
            save_members(
                outgoing,
                {
                    names[path]: metadata_name(path, why)
                    + ", in a folder with nothing else the server accepts"
                    for path, why in plan.metadata.items()
                },
                folder,
                kind="metadata",
            )
            raise AlbumFailed(
                f"Nothing in it the server accepts. Its {plan.total_files} file(s) are in {album_dir}"
                + (
                    "; fix those in invalid/ and send them with --retry-invalid."
                    if plan.invalid
                    else "."
                ),
                kept=album_dir,
            )
        if plan.invalid:
            outgoing = without(
                outgoing, {names[path] for path in plan.invalid}, plan, folder
            )

        def submitted(status_url: str, sent: Outgoing) -> None:
            state.set(
                album_path,
                status="submitted",
                album=plan.name,
                statusUrl=status_url,
                # For an import still running when the run stops: what it sent, to tell
                # the files the server reports skipping by, once the ZIP is gone.
                prefix=list(plan.prefix),
                byName=bool(sent.album),
            )

        job, sent = send(
            server, options, outgoing, plan, folder, year=year, submitted=submitted
        )
        keep_what_the_server_left(job, sent, folder)
        return imported(state, album_path, plan.name, job, folder)
    except (AlbumFailed, requests.RequestException, ValueError) as error:
        failure = (
            error
            if isinstance(error, AlbumFailed)
            else AlbumFailed(f"Unexpected answer: {error}")
        )
        if failure.kept is None:
            failure.kept = album_dir / FAILED_ZIP
            album_dir.mkdir(parents=True, exist_ok=True)
            archive.replace(failure.kept)
            failure.details.append(
                f"What of it is not in {album_dir / 'invalid'} is kept in {failure.kept}."
            )
        if failure is error:
            raise
        raise failure from error
    finally:
        ledger.save()
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
    folder: Folder,
    *,
    year: str,
    submitted: Callable[[str, Outgoing], None],
) -> tuple[dict[str, Any], Outgoing]:
    """Send the ZIP and wait for the server's result; returns it, and what was sent in the end.

    Files the server refuses one by one — content it cannot read, which no listing shows — are
    kept in invalid/ and the rest sent again. An import is all or nothing, so nothing of the
    refused attempt was stored.
    """
    for attempt in range(MAX_RESENDS + 1):
        out.info(
            f"  Sending {human_size(outgoing.path.stat().st_size)} to the server …"
        )
        status_url = server.submit(outgoing.path, year, album=outgoing.album)
        submitted(status_url, outgoing)
        try:
            return wait_for_job(server, options, status_url), outgoing
        except AlbumFailed as failure:
            refused = refused_members(failure.details, outgoing.members)
            if not refused or attempt == MAX_RESENDS:
                raise
            save_members(
                outgoing,
                {name: f"the server: {why}" for name, why in refused.items()},
                folder,
                kind="refused",
            )
            media = set(plan.media)
            if all(
                path not in media or name in refused
                for name, path in outgoing.members.items()
            ):
                save_members(
                    outgoing,
                    {
                        name: metadata_name(path, plan.metadata.get(path, "metadata"))
                        + ", in an album the server refused every photo and video of"
                        for name, path in outgoing.members.items()
                        if name not in refused
                    },
                    folder,
                    kind="metadata",
                )
                raise AlbumFailed(
                    "The server refused every photo and video in it; they are in"
                    f" {folder.dir / 'invalid'}.",
                    failure.details,
                    kept=folder.dir,
                ) from failure
            out.warn(
                f"  The server refused {len(refused)} file(s); keeping them out and sending the rest again:"
            )
            for line in failure.details:
                out.warn(f"      {line}")
            outgoing = without(outgoing, set(refused), plan, folder)
    raise AssertionError("unreachable")


# How the server reports a file it skipped as one it has (photo_album.services).
ALREADY_THERE = "findes allerede i albummet (ignoreret)"
DUPLICATE = re.compile(r"dublet af (?P<earlier>.+) \(ignoreret\)")


def sidecar_target(path: tuple[str, ...]) -> tuple[str, ...] | None:
    """The file an AppleDouble sidecar describes (photo_album.services._sidecar_target): x/._a.jpg,
    x/.AppleDouble/a.jpg and __MACOSX/x/._a.jpg all describe x/a.jpg. None for anything else."""
    parts = [part for part in path if part != "__MACOSX"]
    if len(parts) >= 2 and parts[-2] == ".AppleDouble" and parts[-1] != ".Parent":
        return (*parts[:-2], parts[-1])
    if parts and parts[-1].startswith("._") and len(parts[-1]) > 2:
        return (*parts[:-1], parts[-1][2:])
    return None


def metadata_why(why: str, path: tuple[str, ...], folder: Folder) -> str:
    """Why the server skipped a metadata file, as the overview says it."""
    if why.startswith("Windows-systemfil"):
        return "Windows system file"
    if not why.startswith("macOS-metadata"):
        return f"the server: {why}"
    described = sidecar_target(path)
    if described is None:
        return metadata_name(path, "macOS metadata")
    entry = folder.ledger.get(folder.dropbox(described))
    if entry and entry["kind"] in {"kept-out", "refused"}:
        return f"AppleDouble sidecar of {shown(described)}, which is not on the server either"
    return (
        f"AppleDouble sidecar the server did not keep: it has no file {shown(described)}"
        " to keep it with, or that file has its sidecar already"
    )


def keep_what_the_server_left(
    job: dict[str, Any],
    sent: Outgoing,
    folder: Folder,
    *,
    save: Callable[[dict[str, str]], None] | None = None,
    strike: bool = True,
) -> set[str]:
    """After an import: keep a copy of the metadata the server skipped, and list the duplicates it
    skipped; strike what it stored off the list, deleting any copy of it. Returns the members on
    the server — stored, or skipped as the same file as one it has.

    `save` keeps those copies (from the ZIP sent, unless given); `strike` False leaves the list
    as it is for what was stored, when it is not known exactly what was sent.
    """
    skipped, _ = by_member(job.get("skipped", []), sent.members)
    metadata: dict[str, str] = {}
    on_server: set[str] = set()
    for name, path in sent.members.items():
        why = skipped.get(name)
        duplicate = DUPLICATE.fullmatch(why) if why else None
        if why is None:
            if strike:
                folder.ledger.on_server(folder.dropbox(path))
            on_server.add(name)
        elif why == ALREADY_THERE or duplicate:
            folder.ledger.on_server(folder.dropbox(path))
            earlier = sent.members.get(duplicate["earlier"]) if duplicate else None
            folder.ledger.file(
                folder.dropbox(path),
                "duplicate",
                f"the same file as {shown(earlier)}, which the server stored"
                if earlier
                else "the album on the server had the same file already",
                copy=None,
                size=sent.sizes.get(path, 0),
            )
            on_server.add(name)
        else:
            metadata[name] = metadata_why(why, path, folder)
    if save is None:
        save_members(sent, metadata, folder, kind="metadata")
    else:
        save(metadata)
    for kept_in in sorted(set(KEPT_IN.values())):
        prune(folder.dir / kept_in)
    return on_server


def fetch_what_the_server_left(
    client: dropbox.Dropbox,
    job: dict[str, Any],
    previous: dict[str, Any],
    folder: Folder,
    *,
    options: Options,
) -> None:
    """keep_what_the_server_left for an import sent on an earlier run, whose ZIP is gone: the files
    are told by their names as they were sent, and fetched from Dropbox one by one."""
    prefix, by_name = tuple(previous.get("prefix", [])), previous.get("byName", False)
    folder_name = PurePosixPath(folder.dropbox_path).name
    cut = len(folder.dropbox_path.rstrip("/")) + 1
    sent = Outgoing(Path(), "", {})
    found: dict[tuple[str, ...], Any] = {}
    for entry in list_folder(client, folder.dropbox_path, recursive=True):
        if not isinstance(entry, dropbox.files.FileMetadata):
            continue
        path = PurePosixPath(same_path(entry.path_display[cut:])).parts
        name = shown(below(path, prefix)) if by_name else f"{folder_name}/{shown(path)}"
        sent.members.setdefault(name, path)
        sent.sizes[path] = entry.size
        found[path] = entry

    def save(whys: dict[str, str]) -> None:
        for name, why in whys.items():
            entry, path = found[sent.members[name]], sent.members[name]
            copy = folder.dir.joinpath("metadata", *path)
            if not copy.exists():
                download_file(
                    client,
                    entry.path_display,
                    copy,
                    options.work_dir / DOWNLOAD,
                    entry.client_modified,
                )
            folder.ledger.file(
                folder.dropbox(path), "metadata", why, copy=copy, size=entry.size
            )
        if whys:
            out.info(
                f"  {len(whys)} metadata file(s) saved in {folder.dir / 'metadata'}"
            )

    keep_what_the_server_left(job, sent, folder, save=save, strike=False)


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
    folder: Folder,
) -> str:
    kept_out = len(waiting(folder.dir))
    state.set(
        album_path,
        status="imported",
        album=album,
        albums=[entry["path"] for entry in job.get("albums", [])],
        skipped=job.get("skipped", []),
        keptOut=kept_out,
    )
    folder.ledger.album_imported(album_path)
    folder.ledger.save()
    (folder.dir / FAILED_ZIP).unlink(missing_ok=True)  # of an earlier attempt
    report_job(job, album)
    if kept_out:
        out.warn(
            f"  {kept_out} file(s) were kept out and wait in {folder.dir / 'invalid'}."
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
        out.info(f"    left out: {metadata} metadata file(s), a copy of each kept")


# --- Retrying the kept-out files of one album -----------------------------------------------------


def retry_invalid(
    server: Server, ledger: Ledger, options: Options, album_dir: Path
) -> int:
    """Send the files waiting in an album's invalid/ folder to that album, which they are merged into,
    with the sidecars kept for them in its metadata/ folder.

    Run it once they are fixed: converted, shrunk, or replaced (the folder mirrors the Dropbox one).
    What is still invalid stays where it is; what the server took is deleted.
    """
    try:
        manifest = json.loads((album_dir / "album.json").read_text(encoding="utf-8"))
        year, album, prefix, dropbox_path = (
            manifest["year"],
            manifest["album"],
            tuple(manifest["prefix"]),
            manifest["dropboxPath"],
        )
    except (OSError, ValueError, KeyError) as exc:
        raise Fatal(
            f"{album_dir} is not an album folder this script wrote (no readable album.json: {exc})."
        ) from exc
    folder = Folder(ledger, dropbox_path, album_dir)
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
        if (entry := ledger.get(folder.dropbox(path))) and not options.dry_run:
            entry["why"] = why  # as it is now
    ledger.save()
    if not plan.media:
        out.warn("  Nothing the server would accept yet.")
        return 1 if plan.invalid else 0
    if same_name(album) not in existing:
        out.warn(
            f'  "{year}/{album}" is not on the server; it will be created with these files.'
        )
    # The server keeps a sidecar with its file, and dates the file by it.
    sidecars: dict[tuple[str, ...], Path] = {}
    for path in plan.media:
        for candidate in (
            (*path[:-1], ".AppleDouble", path[-1]),
            (*path[:-1], f"._{path[-1]}"),
        ):
            if (sidecar := album_dir.joinpath("metadata", *candidate)).is_file():
                sidecars[candidate] = sidecar
                break
    if sidecars:
        out.info(f"  … with {len(sidecars)} sidecar(s) from {album_dir / 'metadata'}")
    if options.dry_run:
        return 0

    archive = album_dir.parent / f"{album_dir.name}.retry.zip"
    partial = archive.with_name(archive.name + ".partial")
    sources = {**{path: files[path] for path in plan.media}, **sidecars}
    outgoing = Outgoing(archive, album, {})
    try:
        with zipfile.ZipFile(partial, "w") as zip_file:
            for path, source in sources.items():
                name = shown(below(path, prefix))
                info = zipfile.ZipInfo(name, date_time=zip_time(source.stat().st_mtime))
                with (
                    source.open("rb") as data,
                    zip_file.open(info, "w", force_zip64=True) as copy,
                ):
                    shutil.copyfileobj(data, copy, CHUNK_SIZE)
                outgoing.members[name] = path
                outgoing.sizes[path] = source.stat().st_size
        partial.replace(archive)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise Fatal(f"Cannot write {partial}: {exc}") from exc
    try:
        job, sent = send(
            server,
            options,
            outgoing,
            plan,
            folder,
            year=year,
            submitted=lambda _url, _sent: None,
        )
        report_job(job, album)
        taken = keep_what_the_server_left(job, sent, folder)
    finally:
        archive.unlink(missing_ok=True)
        ledger.save()
    # A fixed file under a new name is on no list, but is the server's now all the same.
    for name in taken:
        sources[sent.members[name]].unlink(missing_ok=True)
    for kept_in in sorted(set(KEPT_IN.values())):
        prune(album_dir / kept_in)
    out.info(f"  {len(taken)} file(s) the server has now, deleted here")
    if left := waiting(album_dir):
        out.warn(f"  {len(left)} file(s) still wait in {invalid_dir}.")
    return 0


# --- Dates for albums already imported ----------------------------------------------------------


def saved_sidecars(album_dir: Path, prefix: tuple[str, ...]) -> dict[str, Path]:
    """The AppleDouble sidecars in an album's metadata/ folder, by their path in the server's album.

    Saved at their paths below the Dropbox folder; made relative to the album the server descended
    to, the way an import's paths are. One outside it describes nothing in the album.
    """
    metadata = album_dir / "metadata"
    sidecars: dict[str, Path] = {}
    for file in sorted(metadata.rglob("*")):
        path = file.relative_to(metadata).parts
        if not file.is_file() or path[: len(prefix)] != prefix:
            continue
        if sidecar_target(path) is not None:
            sidecars[shown(below(path, prefix))] = file
    return sidecars


def against_capture(counts: dict[str, int]) -> str:
    """How well sidecars agree with the capture dates of the photos that have both — what tells
    whether a sidecar can be believed for the photos that have nothing else."""
    near, day, far = (
        counts.get(key, 0) for key in ("withinAMinute", "withinADay", "further")
    )
    if not near + day + far:
        return "No photo with a capture date had a sidecar to compare."
    return (
        f"Against {near + day + far} capture date(s), the sidecar was within a minute for {near},"
        f" within a day for {day}, and further off for {far}."
    )


def strike_kept_sidecars(
    result: dict[str, Any],
    sidecars: dict[str, Path],
    folder: Folder,
    prefix: tuple[str, ...],
) -> int:
    """Delete the copies of the sidecars the server keeps now, and say why it keeps none of the
    rest. Returns how many were deleted."""
    if "kept" not in result:  # a server older than saying so
        out.warn(
            "    The server does not say which sidecars it kept; every copy stays here."
        )
        return 0
    kept = set(result["kept"])
    unmatched, ambiguous = set(result["unmatched"]), set(result["ambiguous"])
    metadata = folder.dir / "metadata"
    for name, file in sidecars.items():
        path = file.relative_to(metadata).parts
        if name in kept:
            folder.ledger.on_server(folder.dropbox(path))
            file.unlink(missing_ok=True)
            continue
        described = shown(sidecar_target(below(path, prefix)) or ())
        why = (
            f"AppleDouble sidecar of {described}, which is not in the album on the server"
            if described in unmatched
            else f"AppleDouble sidecar of {described}: more than one file in the album on the"
            " server has that name"
            if described in ambiguous
            else "AppleDouble sidecar the server could not read"
        )
        folder.ledger.file(
            folder.dropbox(path), "metadata", why, copy=file, size=file.stat().st_size
        )
    prune(metadata)
    folder.ledger.save()
    deleted = len(kept & set(sidecars))
    if deleted:
        out.info(f"    {deleted} sidecar copies deleted here: the server keeps them")
    return deleted


def send_dates(
    server: Server, ledger: Ledger, options: Options, state: State, selected: set[str]
) -> int:
    """--send-dates: every imported album's saved sidecars to the server, which keeps each with its
    photo and dates the photos by them. The server skipped sidecars when most of the archive was
    imported; it reads them on import now, and this gives the albums imported before that the
    same. The copies of the sidecars it kept are deleted."""
    results: dict[str, int] = {
        "albums": 0,
        "updated": 0,
        "redated": 0,
        "deleted": 0,
        "failed": 0,
    }
    for album_path, entry in sorted(state.albums.items()):
        year, folder_name = PurePosixPath(album_path).parts[-2:]
        if entry.get("status") != "imported" or (selected and year not in selected):
            continue
        album_dir = options.work_dir / year / folder_name
        try:
            manifest = json.loads(
                (album_dir / "album.json").read_text(encoding="utf-8")
            )
            album, prefix = manifest["album"], tuple(manifest["prefix"])
            folder = Folder(ledger, manifest["dropboxPath"], album_dir)
        except (OSError, ValueError, KeyError) as exc:
            out.warn(f"{year}/{folder_name}: no readable album.json ({exc}) — skipped.")
            continue
        out.heading(f"{year}/{album}")
        sidecars = saved_sidecars(album_dir, prefix)
        if not sidecars:
            out.info("  No sidecars saved for it.")
            continue
        out.info(f"  {len(sidecars)} sidecar(s) from {album_dir / 'metadata'}")
        if options.dry_run:
            continue
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zip_file:
            for name, file in sidecars.items():
                zip_file.write(file, name)
        try:
            result = server.send_dates(archive.getvalue(), year, album)
        except (AlbumFailed, requests.RequestException, ValueError) as error:
            failure = (
                error
                if isinstance(error, AlbumFailed)
                else AlbumFailed(f"Unexpected answer: {error}")
            )
            out.failure(f"{album_path} (dates)", failure.summary, failure.details)
            results["failed"] += 1
            continue
        out.ok(
            f"  ✓ {result.get('stored', 0)} sidecar(s) stored beside their item;"
            f" {result['updated']} item(s) dated by their sidecar, {result['unchanged']} had that date"
            f" already, {result['undated']} sidecar(s) without a date for {year}; {result['redated']}"
            " item(s) placed anew around them"
        )
        for names, why in (
            (result["unmatched"], "describe no item on the server"),
            (
                result["ambiguous"],
                "describe a name more than one item has (left alone)",
            ),
        ):
            if names:
                more = f" … and {len(names) - 5} more" if len(names) > 5 else ""
                out.warn(
                    f"    {len(names)} sidecar(s) {why}: {', '.join(names[:5])}{more}"
                )
        results["albums"] += 1
        results["updated"] += result["updated"]
        results["redated"] += result["redated"]
        # Absent from a server older than the comparison.
        for closeness, count in result.get("againstCapture", {}).items():
            results[closeness] = results.get(closeness, 0) + count
        out.info(f"    {against_capture(result.get('againstCapture', {}))}")
        results["deleted"] += strike_kept_sidecars(result, sidecars, folder, prefix)
    out.heading("Summary")
    out.info(
        f"  {results['albums']} album(s) sent: {results['updated']} item(s) dated by their sidecar,"
        f" {results['redated']} placed anew around them; {results['deleted']} sidecar copies the"
        " server keeps now deleted here"
    )
    out.info(f"  {against_capture(results)}")
    if results["failed"]:
        out.error(
            f"✗ {results['failed']} album(s) failed; see {options.work_dir / 'failures.log'}."
        )
        return 1
    return 0


# --- Renaming albums named after a folder inside the album folder --------------------------------


@dataclass
class Misnamed:
    """An imported album the server named after a folder inside its Dropbox folder.

    The server descends through every level that holds one folder and no files, and names the album
    after where it stops: "2014/GAHK Color Run", holding a ._.DS_Store and "101GOPRO/", became an
    album "101GOPRO" — nothing on the site says where the videos are from.
    """

    album_path: str  # the Dropbox folder, the state's key
    year: str
    # From the Dropbox folder down to the one the album is named after: ("GAHK Color Run", "101GOPRO").
    folders: tuple[str, ...]
    album: str  # its name on the server

    @property
    def choices(self) -> dict[str, tuple[str, str | None]]:
        """Answer -> (what it does, the name it gives; None to leave the name be)."""
        leaf, parent = self.folders[-1], self.folders[0]
        return {
            "1": ("Skip — ask again next time", None),
            "2": (f"Keep the leaf folder name: {leaf}", leaf),
            "3": (f"Use the parent folder name: {parent}", parent),
            "4": (
                f"Join the folder names: {' '.join(self.folders)}",
                " ".join(self.folders),
            ),
            "5": ("Enter a custom name", None),
        }


def misnamed_albums(state: State, work_dir: Path, selected: set[str]) -> list[Misnamed]:
    """The imported albums whose name is not their Dropbox folder's, and not settled yet.

    The folders in between come from the album's album.json (its `prefix`: the folders the server
    descended through); without one, only the outer folder and the name are known.
    """
    found = []
    for album_path, entry in sorted(state.albums.items()):
        year, folder_name = PurePosixPath(album_path).parts[-2:]
        album = entry.get("album") or folder_name
        if (
            entry.get("status") != "imported"
            or entry.get("nameSettled")
            or (selected and year not in selected)
            or same_name(album) == same_name(folder_name)
        ):
            continue
        prefix: tuple[str, ...] = ()
        try:
            manifest = json.loads(
                (work_dir / year / folder_name / "album.json").read_text(
                    encoding="utf-8"
                )
            )
            prefix = tuple(manifest["prefix"])
        except (OSError, ValueError, KeyError):
            pass
        if not prefix or same_name(prefix[-1]) != same_name(album):
            prefix = (album,)
        found.append(Misnamed(album_path, year, (folder_name, *prefix), album))
    return found


def choose_name(case: Misnamed) -> str | None:
    """Ask what the album is to be called. None: skip it for now."""
    out.info(f"  In Dropbox: {' / '.join(case.folders)}")
    out.info(f"  On the site: {case.year} / {case.album}")
    for key, (label, _) in case.choices.items():
        out.info(f"    {key}) {label}")
    while True:
        answer = input("  Choose 1-5: ").strip()
        if answer not in case.choices:
            print("  Answer 1, 2, 3, 4 or 5.")
            continue
        if answer == "1":
            return None
        if answer == "5":
            custom = input("  New name (empty to go back): ").strip()
            if not custom:
                continue
            return custom
        return case.choices[answer][1]


def renamed_paths(paths: list[str], old: str, new: str) -> list[str]:
    """The state's album paths ("101GOPRO / Sub") with the root renamed."""
    return [
        new + path[len(old) :] if path == old or path.startswith(f"{old} / ") else path
        for path in paths
    ]


def rename_albums(
    server: Server, state: State, options: Options, selected: set[str]
) -> int:
    """--rename-albums: go through the imported albums named after a folder inside their Dropbox
    folder, ask what each is to be called, and rename it on the server — locked or not. The answer
    is remembered: an album kept or renamed is not asked about again; a skipped one is.

    The new name is written to state.json and the album's album.json too, which --retry-invalid
    and --send-dates find the album by."""
    cases = misnamed_albums(state, options.work_dir, selected)
    if not cases:
        out.info("No imported album is named after a folder inside its Dropbox folder.")
        return 0
    out.info(f"{len(cases)} album(s) named after a folder inside their Dropbox folder.")
    if options.dry_run:
        for case in cases:
            out.info(
                f"  {case.year}/{' / '.join(case.folders)}  ->  on the site: {case.album}"
            )
        return 0
    if not sys.stdin.isatty():
        die("--rename-albums asks about each album; run it in a terminal.")
    on_server: dict[str, set[str]] = {}
    results = {"renamed": 0, "kept": 0, "skipped": 0, "missing": 0}
    for case in cases:
        out.heading(f"{case.year}/{case.folders[0]}")
        if case.year not in on_server:
            on_server[case.year], _ = server.albums(case.year)
        if same_name(case.album) not in on_server[case.year]:
            out.warn(
                f'  There is no album "{case.album}" in {case.year} on the site any more (renamed or deleted there?) — skipped.'
            )
            results["missing"] += 1
            continue
        while True:
            name = choose_name(case)
            if name is None:
                out.info("  Skipped; you will be asked again next time.")
                results["skipped"] += 1
                break
            if name == case.album:
                state.update(case.album_path, nameSettled=True)
                out.ok(f'  ✓ Kept as "{name}".')
                results["kept"] += 1
                break
            try:
                renamed = server.rename(case.year, case.album, name)
            except AlbumFailed as failure:
                out.error(f"  ✗ {failure.summary}")
                for line in failure.details:
                    out.error(f"      {line}")
                continue  # ask again: another name may do
            new = renamed["name"]
            entry = state.get(case.album_path)
            state.update(
                case.album_path,
                album=new,
                albums=renamed_paths(entry.get("albums", []), case.album, new),
                renamedFrom=case.album,
                nameSettled=True,
            )
            manifest_path = (
                options.work_dir / case.year / case.folders[0] / "album.json"
            )
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            else:
                manifest["album"] = new
                manifest_path.write_text(
                    json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            on_server[case.year].discard(same_name(case.album))
            on_server[case.year].add(same_name(new))
            out.ok(f'  ✓ Renamed "{case.album}" to "{new}".')
            results["renamed"] += 1
            break
    out.heading("Summary")
    out.info(
        f"  {results['renamed']} renamed, {results['kept']} kept, {results['skipped']} skipped"
        + (f", {results['missing']} not on the site" if results["missing"] else "")
    )
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


def year_folders(
    client: dropbox.Dropbox, root: str, selected: set[str]
) -> tuple[list[str], list[Any]]:
    """The year folders in the Dropbox root to import, and what else is there."""
    try:
        entries = list(list_folder(client, root))
    except AlbumFailed as exc:
        raise Fatal(f"{exc.summary} Is --dropbox-root right?") from exc
    found = sorted(
        entry.name
        for entry in entries
        if isinstance(entry, dropbox.files.FolderMetadata)
        and (YEAR.fullmatch(entry.name) or same_path(entry.name) == OTHER_FOLDER)
    )
    outside = [
        entry
        for entry in entries
        if not (isinstance(entry, dropbox.files.FolderMetadata) and entry.name in found)
    ]
    if selected:
        wanted = {same_path(name) for name in selected}
        if missing := wanted - {same_path(name) for name in found}:
            raise Fatal(f"Not found in {root}: {', '.join(sorted(missing))}")
        found = [name for name in found if same_path(name) in wanted]
    return found, outside


def in_a_year_folder(path: str, root: str) -> bool:
    """Whether a Dropbox path below `root` is in one of its year folders (or Andet)."""
    rest = path[len(root.rstrip("/")) + 1 :]
    folder, separator, _ = rest.partition("/")
    return bool(separator) and (bool(YEAR.fullmatch(folder)) or folder == OTHER_FOLDER)


def album_folders(
    client: dropbox.Dropbox, year_path: str
) -> tuple[list[Any], list[Any]]:
    """The album folders in a year folder, and what else is there: files, and the metadata folders
    an old file server left (.AppleDouble/, __MACOSX/), which are no albums."""
    entries = list(list_folder(client, year_path))
    folders = sorted(
        (
            entry
            for entry in entries
            if isinstance(entry, dropbox.files.FolderMetadata)
            and entry.name not in MACOS_METADATA_FOLDERS
        ),
        key=lambda entry: entry.name,
    )
    outside = [entry for entry in entries if entry not in folders]
    return folders, outside


def metadata_name(path: tuple[str, ...], why: str) -> str:
    """What a metadata file is, for the overview: `why` (classify's), unless it says more."""
    if path[-1] == ".Parent":
        return "macOS metadata of the folder"
    if described := sidecar_target(path):
        return f"AppleDouble sidecar of {shown(described)}"
    return why


def outside_kind(
    path: tuple[str, ...], size: int, rules: Rules, where: str
) -> tuple[str, str]:
    """How the list files something outside the album folders, at `path` below where it lies."""
    kind, why = classify(path, size, rules)
    if kind == "metadata":
        return "metadata", f"{metadata_name(path, why)}, {where}"
    return "outside", where


def keep_outside(
    client: dropbox.Dropbox,
    ledger: Ledger,
    options: Options,
    entries: list[Any],
    *,
    parent: str,
    here: Path,
    rules: Rules,
    where: str,
) -> None:
    """Keep a copy of what lies in `parent` outside its album (or year) folders — `entries`, files
    and folders — in `here`, at its path below `parent`, and list it: the server never sees it.

    Returns the files there, or None if a folder among them could not be listed."""
    files: list[Any] = []
    complete = True
    for entry in entries:
        if isinstance(entry, dropbox.files.FileMetadata):
            files.append(entry)
        elif isinstance(entry, dropbox.files.FolderMetadata):
            try:
                files += [
                    each
                    for each in list_folder(client, entry.path_display, recursive=True)
                    if isinstance(each, dropbox.files.FileMetadata)
                ]
            except AlbumFailed as failure:
                out.failure(entry.path_display, failure.summary, failure.details)
                complete = False
    cut = len(parent.rstrip("/")) + 1
    known = 0
    for entry in sorted(files, key=lambda each: each.path_display):
        if ledger.get(entry.path_display) is not None:
            known += 1  # kept on an earlier run, or dealt with since
            continue
        path = PurePosixPath(same_path(entry.path_display[cut:])).parts
        kind, why = outside_kind(path, entry.size, rules, where)
        copy = here.joinpath(*path)
        if options.dry_run:
            out.warn(f"  {entry.path_display}: {why} — a copy would be kept in {copy}")
            continue
        try:
            download_file(
                client,
                entry.path_display,
                copy,
                options.work_dir / DOWNLOAD,
                entry.client_modified,
            )
        except AlbumFailed as failure:
            out.failure(entry.path_display, failure.summary, failure.details)
            continue
        ledger.file(entry.path_display, kind, why, copy=copy, size=entry.size)
        out.warn(f"  {entry.path_display}: {why} — a copy is kept in {copy}")
    if known:
        out.info(f"  {known} file(s) {where}, kept on an earlier run")
    ledger.save()
    return files if complete else None


def files_below(client: dropbox.Dropbox, path: str) -> list[str] | None:
    """The Dropbox path of every file below `path`: none if there is no such folder, None if it
    could not be listed."""
    try:
        return [
            entry.path_display
            for entry in list_folder(client, path, recursive=True)
            if isinstance(entry, dropbox.files.FileMetadata)
        ]
    except AlbumFailed as failure:
        cause = failure.__cause__
        if (
            isinstance(cause, ApiError)
            and cause.error.is_path()
            and cause.error.get_path().is_not_found()
        ):
            return []
        out.warn(f"  {failure.summary}")
        return None


def strike_moved(
    client: dropbox.Dropbox,
    ledger: Ledger,
    options: Options,
    listed: Collection[str] | None,
    *,
    below: str,
    root: str,
    covers: Callable[[str], bool] = lambda path: True,
) -> None:
    """Strike off the list what Dropbox no longer has below `below` but has at the same place in
    INTERN_DREV — moved out of the archive by move_dropbox_non_media.py — and delete its copy.

    `listed` is every file below `below` now, None if not all of it could be listed (then nothing
    is struck off), and `covers` says which of the list's paths it covers. A file gone from Dropbox
    altogether stays on the list: its copy here may be the last there is.
    """
    if listed is None:
        return
    there = {same_path(path).lower() for path in listed}
    prefix = same_path(below).rstrip("/").lower() + "/"
    gone = [
        path
        for path in ledger.files
        if path.lower().startswith(prefix)
        and covers(path)
        and path.lower() not in there
    ]
    if not gone:
        return
    cut = len(root.rstrip("/"))
    if options.dry_run:
        out.info(
            f"  {len(gone)} file(s) on the list of what is not on the server are no longer in"
            f" {below}; a run strikes off those moved to {INTERN_DREV}"
        )
        return
    moved_to = files_below(client, INTERN_DREV + below.rstrip("/")[cut:])
    if moved_to is None:
        return
    arrived = {same_path(path).lower() for path in moved_to}
    moved = [path for path in gone if (INTERN_DREV + path[cut:]).lower() in arrived]
    for path in moved:
        ledger.moved_out(path)
    ledger.save()
    if moved:
        out.info(
            f"  {len(moved)} file(s) moved to {INTERN_DREV} since: struck off the list of what is"
            " not on the server, their copies here deleted"
        )
    if missing := len(gone) - len(moved):
        out.warn(
            f"  {missing} file(s) on the list of what is not on the server are gone from Dropbox"
            " (not moved there): their copies here are kept, and listed"
        )


def local_files(folder: Path, *, finder: bool = False) -> list[Path]:
    """The files kept below `folder`, not counting what Finder leaves there itself — unless
    `finder`: in metadata/, a .DS_Store is more likely one from Dropbox."""
    if not folder.is_dir():
        return []
    return sorted(
        path
        for path in folder.rglob("*")
        if path.is_file() and (finder or path.name != ".DS_Store")
    )


def reconcile(ledger: Ledger, state: State, rules: Rules) -> None:
    """Start the list from what runs from before there was one kept in the work directory: each
    album folder's invalid/ and metadata/, and the albums that failed."""
    for manifest_file in sorted(ledger.work_dir.glob("*/*/album.json")):
        album_dir = manifest_file.parent
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            folder = Folder(ledger, manifest["dropboxPath"], album_dir)
            prefix = tuple(manifest["prefix"])
        except (OSError, ValueError, KeyError):
            continue
        if album_dir.name in MACOS_METADATA_FOLDERS:
            # A year folder's .AppleDouble/, taken for an album: the metadata of the files beside
            # the albums, kept with them now.
            here = album_dir.parent / NOT_IN_AN_ALBUM / album_dir.name
            for kept_in in sorted(set(KEPT_IN.values())):
                for file in local_files(
                    album_dir / kept_in, finder=kept_in == "metadata"
                ):
                    path = file.relative_to(album_dir / kept_in).parts
                    copy = here.joinpath(*path)
                    copy.parent.mkdir(parents=True, exist_ok=True)
                    file.replace(copy)
                    kind, why = outside_kind(
                        (album_dir.name, *path),
                        copy.stat().st_size,
                        rules,
                        "in the year folder, not in an album folder",
                    )
                    ledger.file(
                        folder.dropbox(path),
                        kind,
                        why,
                        copy=copy,
                        size=copy.stat().st_size,
                    )
            manifest_file.unlink()
            prune(album_dir)
            state.drop(folder.dropbox_path)
            continue
        for file in local_files(album_dir / "invalid"):
            path = file.relative_to(album_dir / "invalid").parts
            kind, why = classify(path, file.stat().st_size, rules)
            if kind == "media":
                kind, why = (
                    "refused",
                    "the server refused it when it read it (import.log says why)",
                )
            else:
                kind = "kept-out"
            ledger.file(
                folder.dropbox(path), kind, why, copy=file, size=file.stat().st_size
            )
        imported = state.get(folder.dropbox_path).get("status") == "imported"
        for file in local_files(album_dir / "metadata", finder=True):
            path = file.relative_to(album_dir / "metadata").parts
            if sidecar_target(path) is not None and path[: len(prefix)] == prefix:
                why = (
                    "AppleDouble sidecar the server did not keep when the album was imported:"
                    " --send-dates sends it"
                    if imported
                    else "AppleDouble sidecar of an album folder not imported: an import sends"
                    " it with the album"
                )
            elif sidecar_target(path) is not None:
                why = "AppleDouble sidecar outside the album the folder makes"
            else:
                why = metadata_name(path, classify(path, file.stat().st_size, rules)[1])
            ledger.file(
                folder.dropbox(path),
                "metadata",
                why,
                copy=file,
                size=file.stat().st_size,
            )
    for album_path, entry in sorted(state.albums.items()):
        if entry.get("status") == "failed":
            errors = entry.get("errors") or ["it failed"]
            album_dir = ledger.work_dir.joinpath(*PurePosixPath(album_path).parts[-2:])
            ledger.album(
                album_path,
                "failed",
                errors[0],
                details=errors[1:],
                copy=album_dir if album_dir.is_dir() else None,
            )


def open_ledger(server: Server, state: State, options: Options) -> Ledger:
    """The list of what is not on the server, started from the work directory if there is none."""
    ledger = Ledger(options.work_dir, read_only=options.dry_run)
    if ledger.new and not options.dry_run:
        _, rules = server.albums(OTHER_FOLDER)
        reconcile(ledger, state, rules)
        ledger.save()
    return ledger


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
        "--year",
        action="append",
        default=[],
        help="only this year folder, or 'Uden årstal' (repeatable)",
    )
    years.add_argument(
        "--year-range", type=year_range, help="only these years, e.g. 2001-2004"
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path(".dropbox-photo-album-import"),
        help="where the state file, the logs, the ZIP in transit and the copies of what is not on"
        " the server live",
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
    parser.add_argument(
        "--send-dates",
        action="store_true",
        help="send the sidecars saved for albums already imported, so the server dates their photos by them",
    )
    parser.add_argument(
        "--rename-albums",
        action="store_true",
        help="go through the imported albums the server named after a folder inside their Dropbox"
        " folder (2014/GAHK Color Run became 101GOPRO), and rename each as you choose;"
        " needs no Dropbox access",
    )
    parser.add_argument(
        "--overview",
        action="store_true",
        help=f"write {OVERVIEW} again from what is known, after you have dealt with some files;"
        " needs no server or Dropbox",
    )
    args = parser.parse_args()

    if args.overview:
        if not (args.work_dir / LEDGER).exists():
            die(
                f"There is no {args.work_dir / LEDGER} yet: it is written by an import."
            )
        report_not_on_server(Ledger(args.work_dir))
        return 0

    if not args.endpoint:
        die("Set PHOTO_ALBUM_IMPORT_URL or pass --endpoint.")
    token = os.environ.get("PHOTO_ALBUM_IMPORT_TOKEN") or die(
        "Set PHOTO_ALBUM_IMPORT_TOKEN."
    )
    selected = args.year_range or set(args.year)
    if invalid := sorted(
        year
        for year in selected
        if not YEAR.fullmatch(year) and same_path(year) != OTHER_FOLDER
    ):
        die(f"Not a year: {', '.join(invalid)}")

    if sum(map(bool, (args.send_dates, args.retry_invalid, args.rename_albums))) > 1:
        die(
            "--send-dates, --retry-invalid and --rename-albums are different runs; pass one of them."
        )
    if args.retry_invalid:
        album_dir = args.retry_invalid
        if album_dir.name == "invalid":
            album_dir = album_dir.parent
        args.work_dir = album_dir.parent.parent  # its logs are that work directory's
    args.work_dir.mkdir(parents=True, exist_ok=True)
    out.open_log(args.work_dir / "import.log", args.work_dir / "failures.log")
    # A ZIP the last run did not finish. Only beside the ZIPs: the album folders hold Dropbox files.
    for leftover in [*args.work_dir.glob("*/*.partial"), args.work_dir / DOWNLOAD]:
        leftover.unlink(missing_ok=True)
    options = Options(
        args.work_dir, args.poll_interval, args.dry_run, args.confirm, args.retry_failed
    )
    server = Server(args.endpoint, token)
    state = State(args.work_dir / "state.json")
    try:
        ledger = open_ledger(server, state, options)
    except (Fatal, AlbumFailed, requests.RequestException) as exc:
        out.failure("the run stopped", str(exc), [], echo=False)
        out.error(f"\n✗ {exc}")
        return 2
    if args.retry_invalid:
        try:
            return retry_invalid(server, ledger, options, album_dir)
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
        finally:
            report_not_on_server(ledger)
    if args.rename_albums:
        try:
            return rename_albums(server, state, options, selected)
        except KeyboardInterrupt:
            out.error(
                "\n✗ Stopped. What you answered is saved; run the same command again for the rest."
            )
            return 130
        except Fatal as exc:
            out.failure("renaming albums", str(exc), [])
            return 2
    if args.send_dates:
        try:
            return send_dates(server, ledger, options, state, selected)
        except KeyboardInterrupt:
            out.error(
                "\n✗ Stopped. Run the same command again; sending an album twice changes nothing."
            )
            return 130
        except Fatal as exc:
            out.failure("sending dates", str(exc), [])
            return 2
        finally:
            ledger.save()
            report_not_on_server(ledger)
    client = dropbox_client()
    results: dict[str, list[str]] = {}
    kept_out: list[tuple[Path, int]] = []

    try:
        out.info(f"Checking {server.endpoint} …")
        # A wrong URL or token stops the run here, not after the Dropbox listing.
        _, rules = server.albums(OTHER_FOLDER)
        out.info(f"Reading {args.dropbox_root} in Dropbox …")
        found, outside = year_folders(client, args.dropbox_root, selected)
        beside_years = keep_outside(
            client,
            ledger,
            options,
            outside,
            parent=args.dropbox_root,
            here=args.work_dir / NOT_IN_A_YEAR,
            rules=rules,
            where="in the Dropbox root, not in a year folder",
        )
        strike_moved(
            client,
            ledger,
            options,
            None
            if beside_years is None
            else [entry.path_display for entry in beside_years],
            below=args.dropbox_root,
            root=args.dropbox_root,
            covers=lambda path: not in_a_year_folder(path, args.dropbox_root),
        )
        for year in found:
            year_path = f"{args.dropbox_root}/{year}"
            existing, rules = server.albums(year)
            albums, outside = album_folders(client, year_path)
            out.heading(
                f"── {year}: {len(albums)} album folder(s), {len(existing)} album(s) already on the server"
            )
            keep_outside(
                client,
                ledger,
                options,
                outside,
                parent=year_path,
                here=args.work_dir / year / NOT_IN_AN_ALBUM,
                rules=rules,
                where="in the year folder, not in an album folder",
            )
            if any(
                path.lower().startswith(f"{same_path(year_path).lower()}/")
                for path in ledger.files
            ):
                strike_moved(
                    client,
                    ledger,
                    options,
                    files_below(client, year_path),
                    below=year_path,
                    root=args.dropbox_root,
                )
            for entry in albums:
                album_path = f"{year_path}/{entry.name}"
                out.heading(f"{year}/{entry.name}")
                try:
                    result = import_album(
                        client,
                        server,
                        state,
                        ledger,
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
                        earlier = args.work_dir / year / entry.name / FAILED_ZIP
                        ledger.album(
                            album_path,
                            "failed",
                            failure.summary,
                            details=failure.details,
                            copy=failure.kept
                            or (earlier if earlier.exists() else None),
                        )
                        ledger.save()
                    result = "failed"
                    if args.stop_on_error:
                        raise Fatal(
                            "Stopped at the first failure (--stop-on-error)."
                        ) from failure
                if result == "exists" and not options.dry_run:
                    ledger.album(
                        album_path,
                        "exists",
                        "an album of that name is on the server already, so the folder was not"
                        " sent; its files were not compared with the album's",
                    )
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
        report_not_on_server(ledger)
        return 130
    except (Fatal, AlbumFailed, requests.RequestException) as exc:
        out.failure("the run stopped", str(exc), [], echo=False)
        out.error(f"\n✗ {exc}")
        out.error("  Progress is saved; run the same command again once this is fixed.")
        report_not_on_server(ledger)
        return 2
    finally:
        ledger.save()
        for leftover in [*args.work_dir.glob("*/*.partial"), args.work_dir / DOWNLOAD]:
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
    report_not_on_server(ledger)
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
