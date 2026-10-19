"""Static checks over every template file.

These are deliberately *static* rather than render-based. The project already had two render-based
guards against leaking template syntax (test_soegvaerelse, test_features), and the bug still reached
production twice more — because a render test only covers the one page it renders, so each new
template is unprotected until someone remembers to add it to a list. Walking the files catches every
template, including ones no test ever visits.
"""

import re
from pathlib import Path

from django.conf import settings

TEMPLATE_DIR = Path(settings.BASE_DIR) / "templates"


def template_files() -> list[Path]:
    files = [p for p in sorted(TEMPLATE_DIR.rglob("*")) if p.is_file()]
    assert files, f"no templates found under {TEMPLATE_DIR} — this check would pass vacuously"
    return files


def test_no_multiline_short_comments() -> None:
    """Django's `{# … #}` is single-line only. Spread it over two lines and the closing `#}` never
    matches, so the whole comment renders verbatim onto the page — which is exactly how a note about
    PurchasePolicy ended up visible to residents on the live ølkælder till.

    Multi-line commentary must use `{% comment %} … {% endcomment %}`.
    """
    offenders = []
    for path in template_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "{#" in line and "#}" not in line.split("{#", 1)[1]:
                rel = path.relative_to(TEMPLATE_DIR).as_posix()
                offenders.append(f"{rel}:{number}: {line.strip()[:80]}")

    assert not offenders, "unterminated {# … #} — use {% comment %} for multi-line:\n" + "\n".join(offenders)


def test_comment_blocks_are_closed() -> None:
    """An unclosed `{% comment %}` swallows the rest of the template silently — the page renders
    truncated rather than erroring, which is easy to miss in review."""
    offenders = []
    for path in template_files():
        text = path.read_text(encoding="utf-8")
        opened, closed = text.count("{% comment %}"), text.count("{% endcomment %}")
        if opened != closed:
            rel = path.relative_to(TEMPLATE_DIR).as_posix()
            offenders.append(f"{rel}: {opened} × comment, {closed} × endcomment")

    assert not offenders, "unbalanced comment blocks:\n" + "\n".join(offenders)


# --- the stylesheet ------------------------------------------------------------------------------
#
# Same reasoning as the template checks above, one directory over: a static read of the source is
# the only thing that can catch these. The built CSS is gitignored, and the bug below is invisible
# to every browser engine except WebKit, so no render test and no Chrome automation will ever see it.

STYLESHEET = Path(settings.BASE_DIR).parent / "frontend" / "src" / "styles.css"


def _css_without_comments() -> str:
    """The stylesheet with /* … */ stripped, so a rule about a property can talk about it freely."""
    text = STYLESHEET.read_text(encoding="utf-8")
    assert text, f"{STYLESHEET} is empty — this check would pass vacuously"
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def test_stylesheet_has_no_webkit_overflow_scrolling() -> None:
    """`-webkit-overflow-scrolling: touch` must never come back.

    On iOS it creates a stacking context AND re-anchors position:fixed descendants to the scroller
    rather than the viewport. It sat on #js-feed, which is an ancestor of every reaction pill, and
    that trapped both `.pop` overlays — the emoji picker and the who-reacted panel — inside the
    feed's layer, where the composer painted over them. Residents on an installed iOS PWA could
    neither see who had reacted nor reach the bottom two rows of the emoji grid.

    It reached production despite a browser pass that walked that exact ancestor chain looking for
    stacking contexts, because the pass ran in Chrome — which ignores the property entirely. Hence
    a source check: it is the only kind that can fail here.

    The property has been a no-op since iOS 13 (momentum scrolling is the default for
    `overflow: auto`) and is gone from the spec, so there is nothing to weigh against removing it.
    """
    offenders = [
        f"line {n}: {line.strip()[:90]}"
        for n, line in enumerate(_css_without_comments().splitlines(), 1)
        if "-webkit-overflow-scrolling" in line
    ]

    assert not offenders, (
        "-webkit-overflow-scrolling traps .pop overlays on iOS — see the .pop block in styles.css:\n"
        + "\n".join(offenders)
    )


def test_reaction_pills_cannot_be_text_selected() -> None:
    """Holding a reaction pill must open the who-reacted panel, not start a selection.

    Without these, iOS treats touch-and-hold on a pill as the start of a text selection and opens
    its copy/define callout, which pre-empts the hold timer in frontend/src/feed.ts — the panel
    never opened and the screen went blue instead. Reported from an installed PWA.

    Asserted here rather than in a browser for the same reason as the check above: it is a WebKit
    behaviour, so a Chrome test cannot tell the fixed version from the broken one.
    """
    css = _css_without_comments()
    start = css.index(".reaction {")
    rule = css[start : css.index("}", start)]

    for prop in ("-webkit-touch-callout:none", "-webkit-user-select:none", "user-select:none"):
        assert prop in rule.replace(" ", ""), f".reaction is missing {prop}"
