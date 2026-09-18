"""Rendering the difference between two versions of a page body, so "hvad blev der egentlig rettet?"
has an answer that does not involve reading two walls of HTML side by side.

stdlib `difflib`; nothing here adds a dependency. The output is deliberately a list of (kind, text)
rows rather than markup: the template escapes every row, because the whole point is to show the body
*HTML as text*, and a diff renderer that emitted safe markup would be one `|safe` away from
executing the very content this project sanitizes on the way in.
"""

import difflib

# Above this, a diff is neither fast nor readable, and the pages this runs on are a few KB. The cap
# exists so that one pathological paste cannot make the history screen unopenable.
MAX_DIFF_CHARS = 200_000

DiffRow = tuple[str, str]  # (kind, text); kind in {"context", "add", "del", "sep"}


def line_diff(old: str, new: str, context: int = 2) -> list[DiffRow]:
    """Unified diff of two texts as (kind, text) rows, with unchanged runs collapsed."""
    if max(len(old), len(new)) > MAX_DIFF_CHARS:
        return [("sep", "Indholdet er for stort til at vise forskelle.")]
    if old == new:
        return []

    rows: list[DiffRow] = []
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), n=context, lineterm="")
    for line in lines:
        if line.startswith(("---", "+++")):
            continue  # filename headers; there are no filenames here
        if line.startswith("@@"):
            rows.append(("sep", "…"))
        elif line.startswith("+"):
            rows.append(("add", line[1:]))
        elif line.startswith("-"):
            rows.append(("del", line[1:]))
        else:
            rows.append(("context", line.removeprefix(" ")))
    return rows
