"""What a website page's address is allowed to look like — the single definition, shared by the
model fields, the redirect table, cms.services and the tests. gahk_web checks the same rules on
every snapshot it receives (its snapshot.py), so the two lists below must stay in step with it.

The rewrite inherited a contradiction that cost a live page: `Page.slug` was a `SlugField`, whose
validator forbids `/`, while the routing contract *requires* it. gahk.dk serves the whole
multi-segment path as one address, and `etl_cms` seeds `faciliteter/kokken` and eight siblings
through `update_or_create`, which skips validators. So the database held addresses the edit form
could never accept back: an editor who changed `faciliteter/kokken` to `faciliteter-kokken` could
not type the old value again, and the page fell out of its section and became unreachable.

Hence a validator that permits exactly the shape gahk.dk serves, and nothing sloppier:

  * two levels at most, matching the legacy routes.php map (`faciliteter/kokken`, never `a/b/c`);
  * lowercase ASCII words separated by `-`/`_`, so an address is typable and stable across the
    301s F-006 promises — this one rule also rejects uppercase, spaces, `.`, `..` and `køkken`;
  * no leading/trailing `/` and no `//`, which are the mistakes a hand-typed path actually makes;
  * not an address gahk.dk uses for something else (RESERVED_FIRST_SEGMENTS, RESERVED_PATHS): a
    page there would never be shown, and gahk.dk refuses a snapshot that contains one.

Deliberately NOT enforced here: uniqueness (the model's `unique=True` plus a friendlier pre-check in
cms.services) and non-emptiness — Django skips validators for empty values, and a page with no
address is legitimate (it is simply not published).
"""

import re

from django.core.exceptions import ValidationError

# Two, because that is the deepest the legacy route map ever went and the section sidebar derives a
# page's parent from `slug.split("/")[0]` — a third level would have no menu to appear in.
MAX_SEGMENTS = 2

SEGMENT_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")

# First path segments gahk.dk (or its webhost) uses for something else. Most of them cannot pass
# SEGMENT_RE anyway (`sw.js`), but the list is kept whole so it reads the same as gahk_web's.
RESERVED_FIRST_SEGMENTS = frozenset(
    {
        "admin",
        "api",
        "begivenheder",
        "billeder",
        "cgi-bin",
        "django-admin",
        "favicon.ico",
        "health",
        "intern",
        "kalender",
        "media",
        "nyintern",
        "photo-album",
        "robots.txt",
        "s",
        "sitemap.xml",
        "static",
        "sw.js",
        "wiki",
        "404",
    }
)

# Whole addresses gahk.dk generates itself (the optagelse forms) or forwards to intern. `optagelse`
# on its own is an ordinary page.
RESERVED_PATHS = frozenset(
    {
        "optagelse/ansoeg",
        "optagelse/fremlej",
        "optagelse/send_rundvisning",
        "optagelse/send_fremleje",
        "optagelse/listansoegninger",
        "optagelse/eksport",
    }
)


def is_reserved(path: str) -> bool:
    """Whether gahk.dk uses `path` for something other than a page."""
    return path.split("/", 1)[0] in RESERVED_FIRST_SEGMENTS or path in RESERVED_PATHS


def validate_page_path(value: str) -> None:
    """Raise ValidationError unless `value` is an address the router can actually resolve."""
    if value != value.strip() or re.search(r"\s", value):
        raise ValidationError("Adressen må ikke indeholde mellemrum.")

    segments = value.split("/")
    if any(not segment for segment in segments):
        raise ValidationError("Adressen må ikke starte eller slutte med “/”, og må ikke indeholde “//”.")

    if len(segments) > MAX_SEGMENTS:
        raise ValidationError(
            "Adressen må højst have to niveauer, fx “faciliteter/kokken”. Du skrev %(count)s niveauer.",
            params={"count": len(segments)},
        )

    for segment in segments:
        if not SEGMENT_RE.match(segment):
            raise ValidationError(
                "“%(segment)s” må kun indeholde små bogstaver (a-z), tal og bindestreg. "
                "Fx “faellesomraade” i stedet for “Fællesområde”.",
                params={"segment": segment},
            )

    # Only the first segment: `faciliteter/media` is a perfectly good address, `media/noget` is not.
    if segments[0] in RESERVED_FIRST_SEGMENTS:
        raise ValidationError(
            "“%(segment)s” er reserveret til en anden del af sitet og kan ikke bruges som adresse.",
            params={"segment": segments[0]},
        )
    if value in RESERVED_PATHS:
        raise ValidationError(
            "/%(path)s bruges af gahk.dk selv og kan ikke bruges som adresse.",
            params={"path": value},
        )


def split_path(value: str | None) -> tuple[str, str]:
    """Split an address into (parent section, last segment): `faciliteter/kokken` -> the two halves.

    A top-level address has no parent, so `vision` -> `("", "vision")`. Total, never raises: it is
    fed pre-fix values straight out of the database, including the illegal ones.
    """
    if not value:
        return "", ""
    parent, _, segment = value.rpartition("/")
    return parent, segment


def join_path(parent: str, segment: str) -> str:
    """Compose an address from the picker's two halves. Empty where either half is missing."""
    if not segment:
        return ""
    return f"{parent}/{segment}" if parent else segment


def normalize_segment(value: str) -> str:
    """Coerce typed input toward a legal segment, so an editor is corrected rather than rejected.

    Deliberately forgiving in a few predictable ways (trim, lowercase, spaces to hyphens) and no
    further: anything still illegal afterwards is a real mistake and must reach
    the editor as an error, not be silently rewritten into a different address than they asked for.
    """
    collapsed = re.sub(r"\s+", "-", value.strip().lower())
    return collapsed.strip("/")
