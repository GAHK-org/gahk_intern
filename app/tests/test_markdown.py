"""core.markdown — the renderer for resident-authored content on opslagstavlen.

Pure unit tests: no database, no client. This module is the security boundary for anything ~100
residents can put on a page every other resident loads, so it is tested on its own, where a failure
points at the renderer and nothing else.

Note the shape of the XSS assertions. They check that dangerous input is *escaped and visible as
text*, not merely that a tag is absent — an absence-only assertion ("`<script>` not in output")
passes even if raw HTML parsing were switched back on, as long as nh3 happened to catch that one
payload. Asserting the escaped form pins down which of the two defences did the work.
"""

import pytest
from django.utils.safestring import SafeString

from core.markdown import extract_image_names, image_sources, plain_text, render_markdown

# --- formatting that must work -------------------------------------------------------------------


def test_basic_formatting_renders() -> None:
    html = render_markdown("**fed** og *kursiv* og `kode`")

    assert "<strong>fed</strong>" in html
    assert "<em>kursiv</em>" in html
    assert "<code>kode</code>" in html


def test_lists_and_blockquotes_render() -> None:
    html = render_markdown("- en\n- to\n\n> citat")

    assert "<ul>" in html and html.count("<li>") == 2
    assert "<blockquote>" in html


def test_a_table_renders_for_the_vaerelsesrunde_results() -> None:
    """Tables are why the `default` preset is used rather than bare CommonMark: the værelsesrunde
    results are the motivating post for this whole feature, and they are a table."""
    html = render_markdown("| Værelse | Beboer |\n|---|---|\n| 003 | Anton |")

    assert "<table>" in html
    assert "<th>Værelse</th>" in html
    assert "<td>003</td>" in html


def test_a_top_level_heading_is_demoted_rather_than_stripped() -> None:
    """`# Overskrift` is the obvious way to write a heading. h1 is not allowed (the page owns its
    single h1), but stripping the tag would leave the text bare and unstyled — which reads as a bug.
    """
    html = render_markdown("# Overskrift")

    assert "<h2>Overskrift</h2>" in html
    assert "<h1" not in html


def test_headings_two_through_four_survive() -> None:
    html = render_markdown("## To\n### Tre\n#### Fire")

    assert "<h2>To</h2>" in html
    assert "<h3>Tre</h3>" in html
    assert "<h4>Fire</h4>" in html


def test_the_result_is_marked_safe() -> None:
    """Templates output this without `|safe`, so it has to arrive already marked."""
    assert isinstance(render_markdown("hej"), SafeString)


@pytest.mark.parametrize("empty", ["", None])
def test_empty_input_renders_empty(empty: str | None) -> None:
    assert render_markdown(empty) == ""


# --- what must never get through -----------------------------------------------------------------


def test_raw_html_is_escaped_not_parsed() -> None:
    """markdown-it runs with html=False, so a script tag never becomes a node — the reader sees the
    characters. Asserting the *escaped* form is the point: `"<script>" not in html` would also pass
    with raw HTML enabled, whenever nh3 happened to strip it."""
    html = render_markdown("<script>alert(1)</script>")

    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_an_event_handler_attribute_cannot_survive() -> None:
    """The payload is escaped wholesale, so `onerror` survives only as visible text — never as a
    live attribute. Asserting on the *tag* is what distinguishes those two."""
    html = render_markdown("<img src=x onerror=alert(1)>")

    assert "&lt;img" in html  # escaped to text...
    assert "<img" not in html  # ...and no real element was created


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "vbscript:msgbox(1)",
    ],
)
def test_a_dangerous_link_scheme_never_becomes_an_href(href: str) -> None:
    html = render_markdown(f"[klik]({href})")

    assert "href" not in html, html


def test_a_data_uri_image_never_becomes_an_element() -> None:
    """markdown-it's own link validator rejects the URI, so it is not even turned into an image; the
    literal markdown stays as text. Assert on the element, not on the substring "data:" — the escaped
    text legitimately contains it."""
    html = render_markdown("![x](data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=)")

    assert "<img" not in html
    assert "src=" not in html


def test_a_same_origin_svg_reference_is_the_upload_layers_problem_not_this_one() -> None:
    """Documents the division of responsibility rather than pretending this layer catches it.

    A hand-typed `/media/…/x.svg` passes the origin filter, because by URL it *is* one of ours. What
    makes it a non-issue is core.uploads: nothing can put an .svg under MEDIA_ROOT in the first
    place. Asserting that here means a future change to the upload allowlist has to come past this
    test and think about it.
    """
    html = render_markdown("![x](/media/opslag/evil.svg)")

    assert '<img src="/media/opslag/evil.svg"' in html


def test_an_offsite_image_is_dropped_entirely() -> None:
    """A remote <img> on an internal page is a tracking pixel: it hands a third party the IP and
    read-time of every resident who opens the post. nh3's url_schemes constrains the scheme but not
    the host, so the attribute filter is the only thing stopping this — and the src-less tag it
    leaves behind is removed too, rather than showing a broken-image icon."""
    html = render_markdown("![sporing](https://evil.example/pixel.gif)")

    assert "evil.example" not in html
    assert "<img" not in html


def test_a_local_image_survives() -> None:
    html = render_markdown("![kaffe](/media/opslag/2026/08/a.jpg)")

    assert '<img src="/media/opslag/2026/08/a.jpg"' in html
    assert 'alt="kaffe"' in html


def test_raw_html_carrying_attributes_is_escaped_wholesale() -> None:
    """A `style="position:fixed"` overlay is the interesting attack on a shared page. With html=False
    the whole tag is escaped to text, so the attribute is never live — visible, but inert."""
    html = render_markdown('<p style="position:fixed" class="x" id="y">t</p>')

    assert html.startswith("<p>&lt;p style=")  # the author's tag is text inside our paragraph
    assert html.count("<p>") == 1  # exactly one real element: ours


def test_the_allowlist_has_no_wildcard_attribute_bucket() -> None:
    """Structural, because it is the thing that keeps style/class/id out of *every* tag and no single
    rendering test would notice it being added. cms/sanitize.py deliberately has one — that allowlist
    is generous for trusted editors preserving migrated page formatting; this one is for everybody."""
    from core.markdown import ALLOWED_ATTRS

    assert "*" not in ALLOWED_ATTRS
    for attrs in ALLOWED_ATTRS.values():
        assert not {"style", "class", "id"} & attrs


def test_links_get_noopener_noreferrer_and_nofollow() -> None:
    html = render_markdown("[gahk](https://gahk.dk)")

    assert 'rel="noopener noreferrer nofollow"' in html


def test_a_pathological_body_still_renders() -> None:
    """A list page renders every post's markdown, so a body that blew up would take out the whole
    board rather than one post. MAX_BODY_CHARS caps the input; this covers the shape."""
    assert render_markdown("> " * 500 + "dybt") is not None
    assert render_markdown("*" * 5000) is not None
    assert render_markdown("[" * 2000 + "x") is not None


# --- plain_text (push bodies) --------------------------------------------------------------------


def test_plain_text_strips_markup_for_a_lock_screen() -> None:
    """A notification body showing `**fed**` and `[link](url)` is why this exists."""
    assert plain_text("**fed** og [et link](https://x.dk)") == "fed og et link"


def test_plain_text_of_nothing_is_empty() -> None:
    assert plain_text(None) == ""
    assert plain_text("") == ""


# --- extract_image_names (the FK claim) ----------------------------------------------------------


def test_referenced_images_are_returned_as_storage_names() -> None:
    """Stripped of MEDIA_URL, so callers can match with an indexed `file__in=` instead of a LIKE."""
    names = extract_image_names("![a](/media/opslag/2026/08/a.jpg) og ![b](/media/opslag/b.png)")

    assert names == {"opslag/2026/08/a.jpg", "opslag/b.png"}


def test_an_image_inside_a_code_fence_is_not_a_reference() -> None:
    """The reason this walks markdown-it's token stream instead of regexing the text. A body-scanning
    sweep cannot tell a code sample from a real reference, so it either leaks files forever or
    eventually deletes a live image."""
    names = extract_image_names("```\n![x](/media/opslag/sample.jpg)\n```")

    assert names == set()


def test_an_inline_code_span_is_not_a_reference() -> None:
    assert extract_image_names("`![x](/media/opslag/sample.jpg)`") == set()


def test_a_remote_image_is_not_claimed() -> None:
    """Only our own uploads have rows to claim."""
    assert extract_image_names("![x](https://evil.example/x.gif)") == set()


# --- the storage-backend round trip ---------------------------------------------------------------
#
# THIS IS THE TEST THAT PROTECTS THE S3 MIGRATION. Everything above feeds extract_image_names a
# hand-written /media/ URL, which stays true no matter what the storage backend does. The pair below
# closes that gap by generating the URL from the backend itself, the way the compose toolbar does
# (opslagstavle.views.upload_image returns `image.file.url`, and opslagstavle.ts writes it into the
# textarea verbatim).
#
# If MediaS3Storage.url() ever starts returning the bucket host — the django-storages default, and
# the obvious "fix" for someone wiring up S3 — these fail. Without them the symptom in production is
# silent: existing opslag images stop rendering, and the next edit of one releases its images for
# purge_notices to delete. Opslagstavlen is still behind its rollout gate, so the blast radius today
# is a trial's worth of posts; it grows to the whole board the day that gate opens. See
# core/storage.py.


@pytest.mark.parametrize(
    "name",
    [
        "opslag/2026/08/a.jpg",
        # Both of these percent-encode in the URL, so they only round-trip because
        # extract_image_names unquotes. get_valid_filename replaces the space but keeps æøå, and
        # "Skærmbillede …" is the DEFAULT DANISH SCREENSHOT NAME — the single most likely thing a
        # resident uploads to opslagstavlen.
        "opslag/2026/08/Skærmbillede_2026-09-04.png",
        "opslag/2026/08/blåbærgrød.png",
    ],
)
def test_storage_urls_round_trip_through_extract_image_names(name: str) -> None:
    """`extract_image_names(storage.url(name))` must return `name`, for every backend.

    The lookup it feeds is `NoticeImage.objects.filter(file__in=names)` — an exact match on the
    FileField name. A URL this cannot be reversed out of does not raise; it just claims nothing.
    """
    from django.core.files.storage import FileSystemStorage

    from core.storage import MediaS3Storage

    filesystem = FileSystemStorage()
    s3 = MediaS3Storage(bucket_name="test-bucket", access_key="k", secret_key="s")

    # The two backends must agree, or a migration changes what gets written into post bodies.
    assert s3.url(name) == filesystem.url(name)
    assert extract_image_names(f"![alt]({s3.url(name)})") == {name}


def test_the_s3_backend_still_exposes_the_presigned_url_separately() -> None:
    """url() is the stored-content URL; signed_url() is the one core.media.serve_media redirects to.

    Asserted so that the override cannot be "simplified" into dropping the presigner altogether,
    which would leave nothing able to actually serve the bytes.
    """
    from core.storage import MediaS3Storage

    # signature_version matches what config/settings.py passes in OPTIONS; without it botocore
    # falls back to the v2 signer, which Hetzner does not accept.
    s3 = MediaS3Storage(bucket_name="test-bucket", access_key="k", secret_key="s", signature_version="s3v4")
    signed = s3.signed_url("opslag/2026/08/a.jpg")

    assert signed.startswith("https://")
    assert "X-Amz-Signature" in signed
    assert not signed.startswith("/media/")


def test_no_images_is_an_empty_set() -> None:
    assert extract_image_names("bare tekst") == set()
    assert extract_image_names("") == set()


# --- image_sources / images-less rendering (the feed excerpt) -------------------------------------
#
# Multi-line markdown is written as triple-quoted literals with real newlines rather than "\n"
# escapes, so what the test feeds the parser is exactly what you read here.

TWO_IMAGES = """![b](/media/opslag/b.jpg)

![a](/media/opslag/a.jpg)"""

REMOTE_AND_LOCAL = """![x](https://evil.example/p.gif)

![y](/media/opslag/y.jpg)"""

FENCED_IMAGE = """```
![x](/media/opslag/x.jpg)
```"""

TEXT_AROUND_IMAGE = """Vigtigt

![a](/media/opslag/a.jpg)

Også vigtigt"""


def test_image_sources_keeps_document_order() -> None:
    """Order matters: the feed uses the *first* image as the thumbnail, so this cannot be a set."""
    assert image_sources(TWO_IMAGES) == ["/media/opslag/b.jpg", "/media/opslag/a.jpg"]


def test_image_sources_counts_only_what_will_render() -> None:
    """A remote src is dropped by the sanitiser, so counting it would collapse a card whose pictures
    the reader never sees."""
    assert image_sources(REMOTE_AND_LOCAL) == ["/media/opslag/y.jpg"]


def test_image_sources_ignores_a_code_fence() -> None:
    assert image_sources(FENCED_IMAGE) == []


def test_image_sources_includes_static_but_extract_names_does_not() -> None:
    """The two differ on purpose: a /static/ image renders (so it counts towards collapsing) but
    ships with the app and has no NoticeImage row to claim."""
    src = "![logo](/static/legacy/x.png)"

    assert image_sources(src) == ["/static/legacy/x.png"]
    assert extract_image_names(src) == set()


def test_rendering_without_images_keeps_the_text() -> None:
    html = render_markdown(TEXT_AROUND_IMAGE, with_images=False)

    assert "<img" not in html
    assert "Vigtigt" in html
    assert "Også vigtigt" in html


def test_rendering_with_images_is_still_the_default() -> None:
    assert "<img" in render_markdown(TEXT_AROUND_IMAGE)


def test_rendering_without_images_still_sanitises() -> None:
    """The excerpt path must not become a way around the allowlist."""
    html = render_markdown("<script>alert(1)</script>", with_images=False)

    assert "&lt;script&gt;" in html
    assert "<script>" not in html
