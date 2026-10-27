#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "dropbox>=12.0",
#     "requests>=2.32",
# ]
# ///
"""Move what is not photos, videos or their metadata out of the photo archive in Dropbox.

Every file under /GAHK billeder that is not a photo, a video, a sidecar or other metadata is moved to
/til_intern_drev, at the same path below it — /GAHK billeder/2015/Papsejladsen 2015/Logo/anker.ai
goes to /til_intern_drev/2015/Papsejladsen 2015/Logo/anker.ai — to be put on the internal drive:
documents, presentations, design files, sound, archives, shortcuts.

What stays:

- Photos and videos, by extension: everything the photo album takes, camera RAW files included,
  whatever their size. Not by asking the server, as import_dropbox_photo_albums.py does: one that
  does not take RAW files yet would have every one of them moved archive.out.
- Sidecars: macOS's AppleDouble files (._name, .AppleDouble/name, __MACOSX/), XMP (.xmp), video
  thumbnails (.thm) and iPhone edits (.aae).
- Other metadata: .DS_Store and every other hidden file, Thumbs.db, desktop.ini, and photo programs'
  own files (ZbThumbnail.info, Picasa.ini, rsmeta.db).

The AppleDouble sidecar of a file that moves goes with it: it is that file's, not a photo's.
--keep adds an extension to what stays: --keep .psd leaves Photoshop files in the archive.

Nothing is overwritten: a file that already has something at its new path stays where it is and is
reported. Everything printed, every move among it, is appended to <work-dir>/til_intern_drev.log.
The import's NOT-ON-SERVER.md still lists what moved, with the copies it keeps, until those copies
are deleted.

Setup: the DROPBOX_* variables of import_dropbox_photo_albums.py, whose Dropbox access this uses.

Usage:
    uv run scripts/move_dropbox_non_media.py                 # list what would move
    uv run scripts/move_dropbox_non_media.py --path 2015     # only below /GAHK billeder/2015
    uv run scripts/move_dropbox_non_media.py --move          # list it, ask, and move it
    uv run scripts/move_dropbox_non_media.py --move --yes    # without asking

Running it again is safe: what has moved is no longer there to move.
"""

import argparse
import sys
import time
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import dropbox

# The import script beside this one: its Dropbox access and retries, its console and log, and what
# macOS metadata is.
import import_dropbox_photo_albums as archive
from dropbox.exceptions import ApiError
from dropbox.files import FileMetadata

# What the photo album takes (photo_album.uploads), camera RAW files included.
PHOTO_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".heic",
        ".heif",
        ".avif",
        ".tif",
        ".tiff",
        ".bmp",
    }
)
RAW_EXTENSIONS = frozenset(
    {
        ".3fr", ".arw", ".cr2", ".cr3", ".crw", ".dng", ".erf", ".iiq", ".mrw", ".nef",
        ".nrw", ".orf", ".pef", ".raf", ".rw2", ".rwl", ".sr2", ".srf", ".srw",
    }
)  # fmt: skip
VIDEO_EXTENSIONS = frozenset(
    {
        ".mp4", ".m4v", ".mov", ".webm", ".avi", ".mkv", ".3gp", ".3g2", ".mts", ".m2ts",
        ".mpg", ".mpeg", ".wmv", ".ogv",
    }
)  # fmt: skip
SIDECAR_EXTENSIONS = frozenset({".xmp", ".thm", ".aae"})
# Not hidden, but metadata all the same: Windows's and photo programs' own files, compared in lower case.
METADATA_FILES = frozenset(
    {
        "thumbs.db",
        "ehthumbs.db",
        "desktop.ini",
        "zbthumbnail.info",
        "picasa.ini",
        "rsmeta.db",
    }
)
# How often a move Dropbox is too busy for is tried again (it locks a folder while writing to it).
BUSY_RETRIES = 5
LOG = "til_intern_drev.log"

Path_ = tuple[str, ...]  # a file's path below the archive's root, folder by folder


@dataclass(frozen=True)
class Move:
    source: str  # its Dropbox path now
    target: str
    size: int
    why: str  # its extension, or whose sidecar it is


def extension(name: str) -> str:
    return PurePosixPath(name).suffix.lower()


def stays(path: Path_, keep: frozenset[str] = frozenset()) -> bool:
    """Whether a file belongs in the photo archive: a photo, a video, a sidecar or metadata."""
    name = path[-1]
    if (
        archive.MACOS_METADATA_FOLDERS & set(path)
        or name.startswith(
            "."
        )  # ._name, .DS_Store, .smbdelete…: macOS's, Samba's and others' hidden files
        or name in archive.MACOS_METADATA_FILES
        or name.lower() in METADATA_FILES
    ):
        return True
    return (
        extension(name)
        in PHOTO_EXTENSIONS
        | RAW_EXTENSIONS
        | VIDEO_EXTENSIONS
        | SIDECAR_EXTENSIONS
        | keep
    )


def to_move(
    files: Collection[Path_], keep: frozenset[str] = frozenset()
) -> dict[Path_, str]:
    """The files that leave the archive, each with why: its extension, or whose sidecar it is."""
    moving = {
        path: extension(path[-1]) or "no extension"
        for path in files
        if not stays(path, keep)
    }
    # Dropbox paths ignore case, and so does a sidecar's match with its file.
    by_lower = {tuple(part.lower() for part in path): path for path in moving}
    for path in files:
        described = archive.sidecar_target(path)
        if described is None or path in moving:
            continue
        if (
            file := by_lower.get(tuple(part.lower() for part in described))
        ) is not None:
            moving[path] = f"sidecar of {file[-1]}"
    return moving


def list_files(
    client: dropbox.Dropbox, root: str, start: str
) -> dict[Path_, tuple[str, int]]:
    """Every file below `start`, by its path below `root`: its Dropbox path, and its size."""
    files: dict[Path_, tuple[str, int]] = {}
    prefix = root.lower() + "/"
    for entry in archive.list_folder(client, start, recursive=True):
        if isinstance(entry, FileMetadata) and entry.path_lower.startswith(prefix):
            files[tuple(entry.path_display[len(prefix) :].split("/"))] = (
                entry.path_display,
                entry.size,
            )
    return files


def plan(
    files: dict[Path_, tuple[str, int]],
    target_root: str,
    keep: frozenset[str] = frozenset(),
) -> list[Move]:
    return sorted(
        (
            Move(files[path][0], f"{target_root}/{'/'.join(path)}", files[path][1], why)
            for path, why in to_move(files, keep).items()
        ),
        key=lambda move: move.source.lower(),
    )


def report(moves: list[Move], source_root: str, target_root: str) -> None:
    by_folder: dict[str, list[Move]] = defaultdict(list)
    for move in moves:
        by_folder[move.source.rpartition("/")[0]].append(move)
    for folder, items in by_folder.items():
        archive.out.info(f"\n{folder}/")
        for move in items:
            archive.out.info(
                f"    {move.source.rpartition('/')[2]}  ({archive.human_size(move.size)}, {move.why})"
            )
    kinds: dict[str, list[Move]] = defaultdict(list)
    for move in moves:
        kinds[
            move.why if not move.why.startswith("sidecar of ") else "sidecars of those"
        ].append(move)
    archive.out.heading(
        f"{len(moves)} file(s), {archive.human_size(sum(move.size for move in moves))}, would move from"
        f" {source_root} to {target_root}:"
    )
    for kind, items in sorted(kinds.items(), key=lambda item: -len(item[1])):
        archive.out.info(
            f"  {kind:20} {len(items):5}  {archive.human_size(sum(move.size for move in items))}"
        )


def move_file(client: dropbox.Dropbox, move: Move) -> str | None:
    """Move one file, never over another. None if it moved; otherwise why not."""
    for attempt in range(1, BUSY_RETRIES + 1):
        try:
            archive.with_dropbox_retries(
                f"move {move.source}",
                lambda: client.files_move_v2(
                    move.source, move.target, autorename=False
                ),
            )
            return None
        except ApiError as exc:
            error = exc.error
            write = (
                error.get_to()
                if error.is_to()
                else error.get_from_write()
                if error.is_from_write()
                else None
            )
            if (
                write is not None
                and write.is_too_many_write_operations()
                and attempt < BUSY_RETRIES
            ):
                time.sleep(2**attempt)
                continue
            if write is not None and write.is_conflict():
                return f"something is already at {move.target}"
            if error.is_from_lookup() and error.get_from_lookup().is_not_found():
                return "it is no longer there"
            return f"Dropbox refused: {error}"
        except (
            archive.AlbumFailed
        ) as failure:  # what with_dropbox_retries gives up with
            return failure.summary
    return "Dropbox stayed too busy to move it"


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--source-root", default="/GAHK billeder", help="the photo archive"
    )
    parser.add_argument(
        "--target-root", default="/til_intern_drev", help="where the rest goes"
    )
    parser.add_argument(
        "--path",
        default="",
        help="only below this folder of the archive: 2015, '2015/Papsejladsen 2015'",
    )
    parser.add_argument(
        "--keep",
        action="append",
        default=[],
        metavar=".EXT",
        help="also leave this extension where it is (repeatable)",
    )
    parser.add_argument(
        "--move", action="store_true", help="move them; without it, only list them"
    )
    parser.add_argument(
        "--yes", action="store_true", help="with --move: do not ask first"
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path(".dropbox-photo-album-import"),
        help="where the log goes",
    )
    args = parser.parse_args()

    source_root, target_root = (
        "/" + args.source_root.strip("/"),
        "/" + args.target_root.strip("/"),
    )
    if f"{source_root.lower()}/".startswith(
        f"{target_root.lower()}/"
    ) or f"{target_root.lower()}/".startswith(f"{source_root.lower()}/"):
        archive.die(f"{source_root} and {target_root} must not be inside one another.")
    keep = frozenset("." + each.lower().lstrip(".") for each in args.keep)
    start = (
        f"{source_root}/{args.path.strip('/')}" if args.path.strip("/") else source_root
    )
    args.work_dir.mkdir(parents=True, exist_ok=True)
    archive.out.open_log(args.work_dir / LOG, args.work_dir / LOG)

    client = archive.dropbox_client()
    archive.out.heading(f"Listing {start} …")
    try:
        files = list_files(client, source_root, start)
    except (archive.AlbumFailed, archive.Fatal) as exc:
        archive.out.error(f"✗ {exc}")
        return 1
    moves = plan(files, target_root, keep)
    archive.out.info(f"{len(files)} file(s) there.")
    if not moves:
        archive.out.ok(
            "Nothing to move: every file is a photo, a video, a sidecar or metadata."
        )
        return 0
    report(moves, source_root, target_root)
    if not args.move:
        archive.out.info("\nNothing moved. Run again with --move to move them.")
        return 0
    if not args.yes and not archive.ask(
        f"\nMove these {len(moves)} file(s) to {target_root}?"
    ):
        archive.out.info("Nothing moved.")
        return 0

    archive.out.heading("Moving …")
    moved, failed = 0, 0
    for move in moves:
        try:
            why_not = move_file(client, move)
        except (
            archive.Fatal
        ) as exc:  # a rejected token: nothing after it would move either
            archive.out.error(f"✗ {exc}")
            failed = len(moves) - moved
            break
        if why_not is None:
            archive.out.info(f"  moved {move.source} → {move.target}")
            moved += 1
        else:
            archive.out.warn(f"  ✗ not moved: {move.source}: {why_not}")
            failed += 1
    archive.out.heading("Summary")
    archive.out.ok(f"  {moved} file(s) moved to {target_root}.")
    if failed:
        archive.out.error(
            f"  ✗ {failed} not moved (see above, or {args.work_dir / LOG})."
        )
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        archive.out.error("\nStopped. What moved has moved; run it again for the rest.")
        sys.exit(130)
