"""Write cms/legacy_images.json: every old-site image file at the pinned commit, with its size and SHA-256.

The import (cms.legacy_images) brings each of these files into the image library, whether content
still uses it or not, and checks every download against this list. The list is made from git history,
not from GitHub, so it needs a clone that has the commit (`git fetch origin main` if it is missing):

    python manage.py legacy_images_manifest            # (re)write the file
    python manage.py legacy_images_manifest --check    # fail if the file differs from git

A development tool: the production image has no git.
"""

import hashlib
import json
import subprocess
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from cms.legacy_images import LEGACY_SOURCE_COMMIT, LEGACY_SOURCE_DIRECTORY, MANIFEST_DIRECTORY, MANIFEST_PATH


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    try:
        return subprocess.run(  # noqa: S603 — fixed git arguments, no shell
            ["git", "-C", str(repo), *args],  # noqa: S607 — git from PATH, a development tool
            input=stdin,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        raise CommandError(
            f"git {' '.join(args[:2])} failed: {detail.decode(errors='replace') or exc}"
        ) from exc


def build_manifest(repo: Path, commit: str = LEGACY_SOURCE_COMMIT) -> dict[str, Any]:
    """The manifest of `commit` in the git repository at `repo`, as the JSON object to write."""
    tree = f"{commit}:{LEGACY_SOURCE_DIRECTORY}{MANIFEST_DIRECTORY}"
    listing = _git(repo, "ls-tree", "-r", "-z", tree)
    blobs: list[tuple[str, str]] = []
    for line in filter(None, listing.split(b"\0")):
        meta, _, name = line.partition(b"\t")
        mode, kind, blob = meta.decode().split()
        if kind != "blob" or mode == "120000":  # a symlink names a path, it holds no picture
            continue
        blobs.append((MANIFEST_DIRECTORY + name.decode("utf-8"), blob))
    files = []
    for path, blob in sorted(blobs):
        data = _git(repo, "cat-file", "blob", blob)
        files.append({"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return {"commit": commit, "directory": LEGACY_SOURCE_DIRECTORY, "files": files}


def manifest_text(manifest: dict[str, Any]) -> str:
    """The file's exact text: one entry per line, so a diff of it is readable."""
    lines = ",\n".join(f"    {json.dumps(entry, ensure_ascii=False)}" for entry in manifest["files"])
    return (
        "{\n"
        f'  "commit": {json.dumps(manifest["commit"])},\n'
        f'  "directory": {json.dumps(manifest["directory"])},\n'
        f'  "files": [\n{lines}\n  ]\n'
        "}\n"
    )


class Command(BaseCommand):
    help = "Write cms/legacy_images.json from git: every old-site image file at the pinned commit."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--check", action="store_true", help="Only compare the file with git; fail if they differ."
        )

    def handle(self, *args: Any, **options: Any) -> None:  # noqa: ANN401
        manifest = build_manifest(Path(settings.BASE_DIR).parent)
        text = manifest_text(manifest)
        total = sum(entry["size"] for entry in manifest["files"])
        summary = f"{len(manifest['files'])} files, {total} bytes, at {LEGACY_SOURCE_COMMIT[:7]}"
        if options["check"]:
            if not MANIFEST_PATH.is_file() or MANIFEST_PATH.read_text("utf-8") != text:
                raise CommandError(f"{MANIFEST_PATH.name} differs from git ({summary}); run without --check")
            self.stdout.write(self.style.SUCCESS(f"{MANIFEST_PATH.name} matches git: {summary}"))
            return
        MANIFEST_PATH.write_text(text, "utf-8")
        self.stdout.write(self.style.SUCCESS(f"Wrote {MANIFEST_PATH}: {summary}"))
