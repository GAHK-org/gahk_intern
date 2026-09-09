"""core.links — finding, shortening and marking up URLs in resident-authored text.

Two halves, and the second one is the point of the feature. The unit tests below pin what a link
becomes; the section at the end pastes one into all six surfaces that carry resident text — a Den
Hurtige message and a thread reply, an opslag and its comment, an event description and its comment
— and asserts the same treatment out of every one. That is the requirement: not "links work here",
but "links work, and identically, wherever anybody types one".

The XSS assertions follow test_markdown.py's shape: they check that dangerous input is escaped and
visible AS TEXT, not merely that a tag is absent. An absence-only assertion passes for the wrong
reason the day somebody makes the escaping conditional.
"""

import datetime
from collections.abc import Callable

import pytest
from django.test import Client
from django.utils import timezone
from django.utils.safestring import SafeString

from core.links import MAX_LINK_TEXT, linkify, shorten
from core.markdown import render_markdown
from residents.models import Resident

# A real-shaped link: long, tracking parameters, the kind people actually paste.
LONG = "https://www.dr.dk/nyheder/politik/en-noget-laengere-overskrift-om-noget?utm_source=x"

# --- shorten -------------------------------------------------------------------------------------


def test_the_scheme_and_www_are_dropped() -> None:
    """The part of a URL that carries no information at all — every reader knows a link is a link.
    Dropping it buys back a third of the budget before anything has to be cut."""
    assert shorten("https://www.gahk.dk/intern") == "gahk.dk/intern"


def test_a_trailing_slash_is_dropped() -> None:
    assert shorten("https://gahk.dk/") == "gahk.dk"


def test_a_short_link_is_left_whole() -> None:
    assert shorten("https://gahk.dk/intern") == "gahk.dk/intern"


def test_a_long_link_is_cut_to_the_budget() -> None:
    text = shorten(LONG)

    assert len(text) == MAX_LINK_TEXT
    assert text.endswith("…")
    # The HOST survives whole — it is the part that answers "should I click this".
    assert text.startswith("dr.dk/nyheder/politik/")


def test_an_email_shows_the_address_not_the_scheme() -> None:
    assert shorten("mailto:anton@gahk.dk") == "anton@gahk.dk"


def test_a_url_that_is_only_a_scheme_is_shown_as_typed() -> None:
    """Rather than as nothing, which is what stripping would leave."""
    assert shorten("https://") == "https://"


# --- linkify: what a link becomes ----------------------------------------------------------------


def test_a_pasted_url_becomes_a_short_link_keeping_the_whole_address() -> None:
    html = linkify(f"Se {LONG} inden fredag")

    assert f'href="{LONG}"' in html  # the full URL, untouched
    assert f'title="{LONG}"' in html  # and again, for a hover
    assert f">{shorten(LONG)}</a>" in html  # but not what the reader sees
    assert LONG not in html.split("</a>")[1]  # the visible half carries no copy of it


def test_a_link_carries_noopener_noreferrer_and_nofollow() -> None:
    """nofollow as much as the other two: these are resident-authored, so the kollegium should not
    pass its search-engine standing to whatever anybody pastes. Django's |urlize did not, which is
    part of why it is gone."""
    assert 'rel="noopener noreferrer nofollow"' in linkify(f"Se {LONG}")


def test_an_email_address_becomes_a_mailto_link() -> None:
    html = linkify("Skriv til anton@gahk.dk hvis du vil med")

    assert 'href="mailto:anton@gahk.dk"' in html
    assert ">anton@gahk.dk</a>" in html


def test_a_link_that_fits_gets_no_tooltip() -> None:
    """A tooltip repeating what is already on screen is noise — and on a phone it is a redundant
    line in the long-press menu, where there is no hover to dismiss it. `mailto:` showing its own
    scheme back was the case that prompted the rule."""
    assert "title=" not in linkify("Skriv til anton@gahk.dk")
    assert "title=" not in linkify("Se https://gahk.dk/intern")


def test_a_link_that_did_not_fit_keeps_the_rest_in_a_tooltip() -> None:
    """The other half: where the cut actually hid something, the tooltip is the only way back."""
    assert f'title="{LONG}"' in linkify(f"Se {LONG}")


def test_a_bare_domain_is_a_link_too() -> None:
    """How people actually type an internal address to each other — nobody writes the scheme."""
    assert 'href="http://gahk.dk/intern"' in linkify("Står på gahk.dk/intern")


def test_a_full_stop_ending_the_sentence_stays_out_of_the_link() -> None:
    html = linkify("Læs den på https://dr.dk/nyheder.")

    assert 'href="https://dr.dk/nyheder"' in html
    assert html.endswith("</a>.")


def test_a_danish_abbreviation_is_not_a_link() -> None:
    """`bl.a.` and `f.eks.` look exactly like a domain to a naive regex. This is most of why the
    scanner is linkify-it rather than twenty minutes of our own."""
    html = linkify("Der er bl.a. kage, og f.eks. kaffe")

    assert "<a" not in html


def test_the_text_around_a_link_is_escaped() -> None:
    html = linkify(f"<script>alert(1)</script> og {LONG}")

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>" not in html


def test_markup_typed_as_text_never_becomes_markup() -> None:
    html = linkify('Se <img src=x onerror="alert(1)"> her')

    assert "&lt;img" in html
    assert "<img" not in html


def test_a_dangerous_scheme_is_not_a_link() -> None:
    """linkify-it does not emit these today. The allowlist is there so that it could not matter if
    it ever did — a scheme is not something escaping changes."""
    for text in ("javascript:alert(1)", "data:text/html,<script>alert(1)</script>"):
        html = linkify(text)
        assert "<a" not in html


def test_quotes_in_a_url_cannot_break_out_of_the_attribute() -> None:
    html = linkify('Se https://gahk.dk/"onmouseover="alert(1) her')

    assert 'onmouseover="alert(1)"' not in html


def test_empty_text_is_empty() -> None:
    assert linkify("") == ""
    assert linkify(None) == ""


def test_the_result_is_a_safestring() -> None:
    """Templates must not add |safe, and they can only skip it if this is already one."""
    assert isinstance(linkify(f"Se {LONG}"), SafeString)
    assert isinstance(linkify("ingen links her"), SafeString)


# --- the Markdown route --------------------------------------------------------------------------
#
# Same rules, reached the other way: a core rule on markdown-it's token stream rather than a scan of
# plain text. What matters is that the two agree.


def test_a_bare_url_in_a_body_is_linked_and_shortened() -> None:
    html = render_markdown(f"Se {LONG} inden fredag")

    assert f'href="{LONG}"' in html
    assert f">{shorten(LONG)}</a>" in html


def test_a_link_the_author_wrote_the_words_for_keeps_them() -> None:
    """Rewriting the text of a link somebody chose the words for would be vandalism. A bare URL has
    no words to lose, which is the whole distinction the token stream is consulted for."""
    html = render_markdown(f"[Læs referatet]({LONG})")

    assert ">Læs referatet</a>" in html
    assert "title=" not in html  # nor a tooltip: the author said what it was


def test_a_short_url_in_a_body_gets_no_tooltip_either() -> None:
    """The Markdown route and the plain-text route have to agree about this too."""
    assert "title=" not in render_markdown("Se https://gahk.dk/intern")


def test_a_url_in_a_code_span_is_not_a_link() -> None:
    html = render_markdown("`https://dr.dk/kode` er en adresse")

    assert "<a" not in html
    assert "<code>https://dr.dk/kode</code>" in html


def test_a_url_in_a_code_fence_is_not_a_link() -> None:
    html = render_markdown("```\nhttps://dr.dk/blok\n```")

    assert "<a" not in html


def test_an_angle_bracket_autolink_is_shortened_too() -> None:
    html = render_markdown(f"<{LONG}>")

    assert f">{shorten(LONG)}</a>" in html


def test_a_lock_screen_gets_the_short_form() -> None:
    """plain_text feeds the push body, where the full URL was 120 characters of a 140-character
    preview. A pleasant accident of shortening at the token level rather than in CSS."""
    from core.markdown import plain_text

    assert plain_text(f"Se {LONG}") == f"Se {shorten(LONG)}"


# --- every surface, one URL ----------------------------------------------------------------------
#
# The requirement, asserted where a reader meets it. Everything above could pass while a template
# still rendered `{{ body }}` raw, which is exactly the state three of these five were in.


@pytest.fixture
def _open_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both boards ship ungated, but their own tests keep a switch; do the same rather than depend
    on the shipped value."""
    from den_hurtige import access as hurtig_access
    from opslagstavle import access as board_access

    monkeypatch.setattr(hurtig_access, "ACCESS_ROLES", None)
    monkeypatch.setattr(board_access, "ACCESS_ROLES", None)


def _url(slug: str) -> str:
    """A distinct real-shaped URL per surface.

    ONE URL FOR ALL SIX WOULD NOT BE A TEST. Several of these surfaces share a page — the thread
    panel renders the parent message above the replies, a notice renders above its comments — so a
    single address would let one working surface satisfy the assertion for a broken one sitting
    beside it. Giving each its own is what makes six assertions rather than four.
    """
    return f"https://www.dr.dk/nyheder/{slug}/en-noget-laengere-overskrift?utm_source=x"


@pytest.mark.django_db
def test_a_pasted_link_is_clickable_and_short_on_every_surface(
    client: Client, make_resident: Callable[..., Resident], _open_gates: None
) -> None:
    """The requirement, asserted where a reader meets it. Everything above could pass while a
    template still printed `{{ body }}` raw — which is the state three of these six were in.

    Each entry is (what it is, the page it is read on, the URL pasted into it).
    """
    from den_hurtige.models import QuickComment, QuickPost
    from events.models import Event, EventComment
    from opslagstavle.models import Notice, NoticeComment

    author = make_resident(email="link@gahk.dk", first_name="Link", last_name="Tester")
    client.force_login(author)

    post = QuickPost.objects.create(author=author, content=f"Se {_url('besked')}")
    QuickComment.objects.create(post=post, author=author, content=f"Også her {_url('svar')}")
    notice = Notice.objects.create(author=author, body=f"Referatet ligger på {_url('opslag')}")
    NoticeComment.objects.create(notice=notice, author=author, body=f"Tak {_url('opslag-svar')}")
    event = Event.objects.create(
        organiser=author,
        title="Fællesspisning",
        starts_at=(timezone.now() + datetime.timedelta(days=3)).replace(second=0, microsecond=0),
        description=f"Tilmelding på {_url('begivenhed')}",
    )
    EventComment.objects.create(event=event, author=author, body=f"Er det {_url('begivenhed-svar')}?")

    feed = "/intern/den-hurtige/"
    surfaces = [
        ("Den Hurtige: en besked", feed, _url("besked")),
        ("Den Hurtige: et trådsvar", f"{feed}{post.pk}/traad", _url("svar")),
        ("Den Hurtige: beskeden over tråden", f"{feed}{post.pk}/traad", _url("besked")),
        ("opslagstavlen: selve opslaget", f"/intern/opslagstavle/{notice.pk}", _url("opslag")),
        ("opslagstavlen: en kommentar", f"/intern/opslagstavle/{notice.pk}", _url("opslag-svar")),
        ("begivenheder: beskrivelsen", f"/intern/begivenheder/{event.pk}", _url("begivenhed")),
        ("begivenheder: en kommentar", f"/intern/begivenheder/{event.pk}", _url("begivenhed-svar")),
    ]

    bodies = {url: client.get(url).content.decode() for _, url, _ in surfaces}
    for what, page, url in surfaces:
        body = bodies[page]
        assert f'href="{url}"' in body, f"not clickable — {what}"
        assert f">{shorten(url)}</a>" in body, f"not shortened — {what}"
        assert url not in body.replace(f'href="{url}"', "").replace(f'title="{url}"', ""), (
            f"the raw URL is still printed as text — {what}"
        )
