#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "dropbox>=12.0",
#     "requests>=2.32",
# ]
# ///
"""Import /GAHK billeder from Dropbox, building and deleting one annual ZIP at a time."""

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
    client: dropbox.Dropbox, path: str
) -> Iterator[dropbox.files.Metadata]:
    listing = client.files_list_folder(
        path, recursive=True, include_non_downloadable_files=False
    )
    yield from listing.entries
    while listing.has_more:
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
        return [year for year in years if year in selected_years]
    return years


def build_zip(client: dropbox.Dropbox, source: str, target: Path) -> int:
    files = [
        entry
        for entry in iter_entries(client, source)
        if isinstance(entry, dropbox.files.FileMetadata)
    ]
    with zipfile.ZipFile(
        target, "w", compression=zipfile.ZIP_STORED, allowZip64=True
    ) as archive:
        for index, entry in enumerate(files, start=1):
            relative_name = entry.path_display.removeprefix(f"{source}/")
            print(f"  Downloading {index}/{len(files)}: {relative_name}", flush=True)
            _, response = client.files_download(entry.path_display)
            with response, archive.open(relative_name, "w") as destination:
                shutil.copyfileobj(response.raw, destination, length=CHUNK_SIZE)
    return len(files)


def submit_and_wait(
    zip_path: Path, year: str, endpoint: str, token: str, poll_interval: float
) -> None:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    with zip_path.open("rb") as archive:
        response = requests.post(
            endpoint,
            headers=headers,
            data={"folder": year},
            files={"archive": (zip_path.name, archive, "application/zip")},
            timeout=(30, None),
        )
    response.raise_for_status()
    status_url = urljoin(endpoint, response.json()["statusUrl"])
    print("  ZIP uploaded; waiting for server import.", flush=True)
    while True:
        response = requests.get(status_url, headers=headers, timeout=30)
        response.raise_for_status()
        job = response.json()
        if job["state"] == "ready":
            return
        if job["state"] == "failed":
            raise RuntimeError(f"Server import failed: {job['error']}")
        time.sleep(poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=os.environ.get("PHOTO_ALBUM_IMPORT_URL"))
    parser.add_argument("--dropbox-root", default="/GAHK billeder")
    parser.add_argument(
        "--year",
        action="append",
        default=[],
        help="Import only this YYYY folder; repeatable",
    )
    parser.add_argument(
        "--work-dir", type=Path, default=Path(".dropbox-photo-album-import")
    )
    parser.add_argument("--poll-interval", type=float, default=5.0)
    args = parser.parse_args()

    if not args.endpoint:
        raise SystemExit("Set PHOTO_ALBUM_IMPORT_URL or pass --endpoint.")
    selected_years = set(args.year)
    if invalid_years := [year for year in selected_years if not YEAR.fullmatch(year)]:
        raise SystemExit(f"Invalid year: {', '.join(sorted(invalid_years))}")

    client = dropbox.Dropbox(required_environment("DROPBOX_ACCESS_TOKEN"))
    token = required_environment("PHOTO_ALBUM_IMPORT_TOKEN")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    for year in year_folders(client, args.dropbox_root, selected_years):
        zip_path = args.work_dir / f"{year}.zip"
        if zip_path.exists():
            raise SystemExit(
                f"Refusing to overwrite {zip_path}; finish or remove it before retrying."
            )
        print(f"Importing {year}", flush=True)
        try:
            count = build_zip(client, f"{args.dropbox_root}/{year}", zip_path)
            submit_and_wait(zip_path, year, args.endpoint, token, args.poll_interval)
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
