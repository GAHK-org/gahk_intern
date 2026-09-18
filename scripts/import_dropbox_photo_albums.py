#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "dropbox>=12.0",
#     "requests>=2.32",
# ]
# ///
"""Import /GAHK billeder from Dropbox, building and deleting one album ZIP at a time."""

import argparse
import os
import re
import shutil
import sys
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urljoin

import dropbox
import requests


YEAR = re.compile(r"^\d{4}$")
CHUNK_SIZE = 1024 * 1024


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Set {name} before running this script.")
    return value


def iter_entries(
    client: dropbox.Dropbox, path: str, *, recursive: bool = False
) -> Iterator[dropbox.files.Metadata]:
    print(f"Listing {path} ...", flush=True)
    listing = client.files_list_folder(
        path, recursive=recursive, include_non_downloadable_files=False
    )
    yield from listing.entries
    while listing.has_more:
        print(f"  Fetching another page from {path} ...", flush=True)
        listing = client.files_list_folder_continue(listing.cursor)
        yield from listing.entries


def year_folders(
    client: dropbox.Dropbox, root: str, selected_years: set[str]
) -> list[str]:
    entries = list(iter_entries(client, root))
    years = sorted(
        entry.name
        for entry in entries
        if isinstance(entry, dropbox.files.FolderMetadata)
        and entry.path_display
        and entry.path_display.count("/") == root.count("/") + 1
        and YEAR.fullmatch(entry.name)
    )
    if selected_years:
        missing = selected_years - set(years)
        if missing:
            raise SystemExit(
                f"Year folders not found in Dropbox: {', '.join(sorted(missing))}"
            )
        years = [year for year in years if year in selected_years]
    print(
        f"Found {len(years)} year folder(s): {', '.join(years) or 'none'}", flush=True
    )
    return years


def album_folders(client: dropbox.Dropbox, year_path: str) -> list[str]:
    albums = sorted(
        entry.name
        for entry in iter_entries(client, year_path)
        if isinstance(entry, dropbox.files.FolderMetadata)
    )
    print(
        f"Found {len(albums)} album folder(s) in {year_path}: {', '.join(albums) or 'none'}",
        flush=True,
    )
    return albums


def download_zip(client: dropbox.Dropbox, source: str, target: Path) -> int:
    print(f"Requesting a Dropbox ZIP for {source} ...", flush=True)
    _, response = client.files_download_zip(source)
    with response, target.open("wb") as destination:
        shutil.copyfileobj(response.raw, destination, length=CHUNK_SIZE)
    count = flatten_archive_root(target)
    print(f"  Downloaded {target} with {count} file(s).", flush=True)
    return count


def flatten_archive_root(target: Path) -> int:
    with zipfile.ZipFile(target) as archive:
        members = archive.infolist()
        files = [member for member in members if not member.is_dir()]
        root_directories = {
            Path(member.filename).parts[0]
            for member in files
            if len(Path(member.filename).parts) > 1
        }
        if len(root_directories) != 1 or len(root_directories) != len(files):
            return len(files)

        root = root_directories.pop()
        normalized = target.with_suffix(".normalized.zip")
        print(f"  Removing Dropbox archive wrapper {root}/ ...", flush=True)
        with zipfile.ZipFile(normalized, "w", compression=zipfile.ZIP_STORED) as output:
            for member in files:
                relative_path = Path(*Path(member.filename).parts[1:]).as_posix()
                with (
                    archive.open(member) as source,
                    output.open(relative_path, "w") as destination,
                ):
                    shutil.copyfileobj(source, destination, length=CHUNK_SIZE)
    normalized.replace(target)
    return len(files)


def submit_and_wait(
    zip_path: Path, year: str, endpoint: str, token: str, poll_interval: float
) -> None:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    system_endpoint = endpoint.rstrip("/")
    if not system_endpoint.endswith("/system"):
        system_endpoint = f"{system_endpoint}/system"
    print(f"  Sending {zip_path.name} to {system_endpoint} ...", flush=True)
    with zip_path.open("rb") as archive:
        response = requests.post(
            system_endpoint,
            headers=headers,
            data={"folder": year},
            files={"archive": (zip_path.name, archive, "application/zip")},
            timeout=(30, None),
        )
    response.raise_for_status()
    status_url = urljoin(system_endpoint, response.json()["statusUrl"])
    print(f"  ZIP uploaded; polling {status_url}.", flush=True)
    last_state: str | None = None
    while True:
        print("  Requesting import status ...", flush=True)
        response = requests.get(status_url, headers=headers, timeout=30)
        response.raise_for_status()
        job = response.json()
        if job["state"] != last_state:
            print(f"  Server import state: {job['state']}", flush=True)
            last_state = job["state"]
        if job["state"] == "ready":
            return
        if job["state"] == "failed":
            raise RuntimeError(f"Server import failed: {job['error']}")
        time.sleep(poll_interval)


def year_range(value: str) -> set[str]:
    start, separator, end = value.partition("-")
    if not separator or not YEAR.fullmatch(start) or not YEAR.fullmatch(end):
        raise argparse.ArgumentTypeError("must use YYYY-YYYY, for example 2001-2004")
    if start > end:
        raise argparse.ArgumentTypeError("must end with the same or a later year")
    return {str(year) for year in range(int(start), int(end) + 1)}


def should_upload(year: str, album: str) -> bool:
    while True:
        answer = input(f"Upload {year}/{album}? [y]es/[s]kip: ").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"s", "skip"}:
            return False
        print("Enter y to upload or s to skip.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=os.environ.get("PHOTO_ALBUM_IMPORT_URL"))
    parser.add_argument("--dropbox-root", default="/GAHK billeder")
    year_selection = parser.add_mutually_exclusive_group()
    year_selection.add_argument(
        "--year",
        action="append",
        default=[],
        help="Import only this YYYY folder; repeatable",
    )
    year_selection.add_argument(
        "--year-range",
        type=year_range,
        help="Import YYYY folders in this inclusive range, for example 2001-2004",
    )
    parser.add_argument(
        "--work-dir", type=Path, default=Path(".dropbox-photo-album-import")
    )
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Ask whether to upload or skip each album",
    )
    args = parser.parse_args()

    if not args.endpoint:
        raise SystemExit("Set PHOTO_ALBUM_IMPORT_URL or pass --endpoint.")
    selected_years = args.year_range or set(args.year)
    if invalid_years := [year for year in selected_years if not YEAR.fullmatch(year)]:
        raise SystemExit(f"Invalid year: {', '.join(sorted(invalid_years))}")

    client = dropbox.Dropbox(required_environment("DROPBOX_ACCESS_TOKEN"))
    token = required_environment("PHOTO_ALBUM_IMPORT_TOKEN")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    print(f"Connecting to {args.dropbox_root} ...", flush=True)
    for year in year_folders(client, args.dropbox_root, selected_years):
        year_path = f"{args.dropbox_root}/{year}"
        for album in album_folders(client, year_path):
            if args.confirm and not should_upload(year, album):
                print(f"Skipped {year}/{album}.", flush=True)
                continue

            zip_path = args.work_dir / year / f"{album}.zip"
            if zip_path.exists():
                raise SystemExit(
                    f"Refusing to overwrite {zip_path}; finish or remove it before retrying."
                )
            album_path = f"{year_path}/{album}"
            print(f"Importing folder {album_path}", flush=True)
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                count = download_zip(client, album_path, zip_path)
                submit_and_wait(
                    zip_path, year, args.endpoint, token, args.poll_interval
                )
            except Exception:
                print(
                    f"  Failed; keeping {zip_path} for inspection or retry.",
                    file=sys.stderr,
                )
                raise
            zip_path.unlink()
            print(f"  Imported {count} file(s); removed {zip_path}.", flush=True)


if __name__ == "__main__":
    main()
