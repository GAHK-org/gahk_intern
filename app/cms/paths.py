"""What a CMS page's address is allowed to look like — the single definition, shared by the admin
form, the model field, the redirect table, the system check and the tests.

The rewrite inherited a contradiction that cost a live page: `Page.slug` was a `SlugField`, whose
validator forbids `/`, while the routing contract *requires* it. `config.urls` matches the whole
multi-segment path as one lookup key, and `etl_cms` seeds `faciliteter/kokken` and eight siblings
through `update_or_create`, which skips validators. So the database held addresses the edit form
could never accept back: an editor who changed `faciliteter/kokken` to `faciliteter-kokken` could
not type the old value again, and the page fell out of its section sidebar (the only thing that
links a sub-page — see cms.nav) and became unreachable.

Hence a validator that permits exactly the shape the router resolves, and nothing sloppier:

  * two levels at most, matching the legacy routes.php map (`faciliteter/kokken`, never `a/b/c`);
  * lowercase ASCII words separated by `-`/`_`, so an address is typable and stable across the
    /301s F-006 promises — this one rule also rejects uppercase, spaces, `.`, `..` and `køkken`;
  * no leading/trailing `/` and no `//`, which are the mistakes a hand-typed path actually makes;
  * a first segment that is not already mounted in the URLconf. A fixed pattern is matched before
    the catch-all, so a page addressed `intern` would simply never open, with nothing reporting it
    (cms.checks keeps this list honest as urls.py grows).

Deliberately NOT enforced here: uniqueness (the model's `unique=True` plus a friendlier pre-check in
PageAdminForm) and non-emptiness — Django skips validators for empty values, and a page with no
address is legitimate (the `optagelse` bodies, which that app renders itself).
"""

import re

from django.core.exceptions import ValidationError

# Two, because that is the deepest the legacy route map ever went and the section sidebar derives a
# page's parent from `slug.split("/")[0]` — a third level would have no menu to appear in.
MAX_SEGMENTS = 2

SEGMENT_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")

# First path segments owned by something other than the CMS catch-all. Matched patterns win over the
# catch-all, so a page given one of these addresses is unreachable rather than merely shadowed.
# `cms.checks.check_reserved_top_segments` fails the build if urls.py grows a segment missing here.
RESERVED_TOP_SEGMENTS = frozenset(
    {
        "admin",
        "django-admin",
        "intern",
        "nyintern",
        "optagelse",
        "begivenheder",
        "kalender",
        "media",
        "static",
        "sw.js",
        "favicon.ico",
        "robots.txt",
    }
)


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
    if segments[0] in RESERVED_TOP_SEGMENTS:
        raise ValidationError(
            "“%(segment)s” er reserveret til en anden del af sitet og kan ikke bruges som adresse.",
            params={"segment": segments[0]},
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

    Deliberately forgiving in exactly the ways `page_path.js` previews live (trim, lowercase, spaces
    to hyphens) and no further: anything still illegal afterwards is a real mistake and must reach
    the editor as an error, not be silently rewritten into a different address than they asked for.
    """
    collapsed = re.sub(r"\s+", "-", value.strip().lower())
    return collapsed.strip("/")
