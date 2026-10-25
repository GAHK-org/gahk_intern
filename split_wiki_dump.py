"""Split a full phpMyAdmin export of `gahk_dk` into its app half and its MediaWiki half.

one.com's MySQL host (`gahk.dk.mysql.service.one.com`) only resolves inside their hosting network, so
the dump has to come out through phpMyAdmin as one whole-database export — there is no way to run
`mysqldump --ignore-table` remotely. This does the `wiki*` carve-out (DEPLOY.md §5) locally instead.

phpMyAdmin groups its output into blocks introduced by a three-line comment header:

    --
    -- Table structure for table `wikipage`
    --

Every line is attributed to the table named by the most recent such header. Lines that belong to no
table — the preamble (SET NAMES / charset saves), the "for dumped tables" group headers, COMMIT and
the trailing charset restores — are emitted into *both* halves, so each output is a valid standalone
dump. Works on bytes, never decoding, so mixed latin1/utf8mb3/binary column data passes through
untouched.

    python split_wiki_dump.py gahk_dk.sql --mode wiki --out wiki.sql
    python split_wiki_dump.py gahk_dk.sql --mode app  --out gahk_dk-app.sql
    python split_wiki_dump.py gahk_dk.sql --mode report
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# "-- Table structure for table `x`", "-- Indexes for table `x`", "-- Dumping data for table `x`", …
# Views must be matched too ("Stand-in structure for view `x`" early, "Structure for view `x`" at the
# end). The real CREATE VIEW trails the last ALTER TABLE, so without this it inherits whichever table
# happened to come last and lands in the wrong half.
HEADER = re.compile(rb"^-- .+ for (?:table|view) `([^`]+)`\s*$")
# "-- Indexes for dumped tables" etc. — group headers that belong to no single table.
GROUP_HEADER = re.compile(rb"^-- [A-Za-z_ ]+ for dumped tables\s*$")


def split(
    src: Path, prefix: bytes
) -> tuple[list[bytes], list[bytes], list[bytes], list[bytes]]:
    """Return (shared, wiki_lines, app_lines, table_names) for one pass over the dump."""
    shared: list[bytes] = []
    wiki: list[bytes] = []
    app: list[bytes] = []
    seen: list[bytes] = []
    current: bytes | None = None

    with src.open("rb") as fh:
        for raw in fh:
            line = raw.rstrip(b"\r\n")
            if m := HEADER.match(line):
                current = m.group(1)
                if current not in seen:
                    seen.append(current)
            elif GROUP_HEADER.match(line) or line.startswith(b"COMMIT;"):
                # Group headers and the final COMMIT end the previous table's block; everything from
                # COMMIT onward is the footer and belongs to both halves.
                current = None

            if current is None:
                shared.append(raw)
            elif current.startswith(prefix):
                wiki.append(raw)
            else:
                app.append(raw)

    return shared, wiki, app, seen


def write_half(out: Path, shared: list[bytes], body: list[bytes], src: Path) -> None:
    """Interleaving is not needed: the shared lines are preamble-then-footer, and the body sits
    between them. Find the split point by locating the footer (everything from the last COMMIT)."""
    footer_at = next(
        (i for i, ln in enumerate(shared) if ln.startswith(b"COMMIT;")),
        len(shared),
    )
    with out.open("wb") as fh:
        fh.writelines(shared[:footer_at])
        fh.writelines(body)
        fh.writelines(shared[footer_at:])
    print(f"  wrote {out}  ({out.stat().st_size / 1e6:.1f} MB, from {src.name})")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("dump", type=Path, help="full phpMyAdmin export of gahk_dk")
    p.add_argument("--mode", choices=("wiki", "app", "report"), default="report")
    p.add_argument(
        "--out", type=Path, help="output file (required for --mode wiki/app)"
    )
    p.add_argument(
        "--prefix",
        default="wiki",
        help="table-name prefix treated as MediaWiki's (default: wiki)",
    )
    args = p.parse_args()

    if not args.dump.is_file():
        print(f"error: {args.dump} not found", file=sys.stderr)
        return 1
    if args.mode in ("wiki", "app") and not args.out:
        print("error: --out is required with --mode wiki/app", file=sys.stderr)
        return 1

    prefix = args.prefix.encode()
    shared, wiki, app, seen = split(args.dump, prefix)
    wiki_tables = [t for t in seen if t.startswith(prefix)]
    app_tables = [t for t in seen if not t.startswith(prefix)]

    # ASCII only: the Windows console codepage mangles non-ASCII here.
    print(
        f"{args.dump.name}: {len(seen)} tables - {len(app_tables)} app, {len(wiki_tables)} wiki"
    )
    print(f"  shared header/footer lines: {len(shared)}")
    print(f"  app body lines:  {len(app)}")
    print(f"  wiki body lines: {len(wiki)}")

    if args.mode == "report":
        print("\napp tables:")
        print("  " + ", ".join(t.decode() for t in app_tables))
        print("\nwiki tables:")
        print("  " + ", ".join(t.decode() for t in wiki_tables))
        return 0

    write_half(args.out, shared, wiki if args.mode == "wiki" else app, args.dump)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
