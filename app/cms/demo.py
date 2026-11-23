"""The Hjemmeside's made-up history for `manage.py seed_demo` (a part of audit.demo's timeline).

The seed writes the website's content unaudited, so without this the change log and the audit
page's Hjemmeside area would be empty on a fresh checkout. These five weeks of editing make every
part of the log worth looking at:

  * every type in its filter: pages, news, front-page events, an image, the menu, an old address,
    the settings and gahk.dk itself;
  * every person in its filter: formand@, admin@, a member of Indstillingen and one of Inspektionen,
    and System, a fix made from a shell, which the log marks as made outside the editor;
  * texts changed as line diffs, a page created and one renamed (its old address keeps working), a
    page edited twice and then put back to the earlier version (a restore, with its version);
  * the menu reordered (the list before and after), news and events created, edited and deleted;
  * the bin (cms.bin) with one of each kind in it: a news item, an event, an image, an old address
    and a menu entry binned days ago (not yet deletable for good), and a page with its old address
    binned over a month ago, which an administrator may now delete permanently;
  * the latest change only hours old, so a page's editor has a recent "saved by" to name.

It goes through cms.services, so each event is the one the editor writes, and the services' own
bookkeeping (versions, "last changed", upload times) is dated to the event afterwards. The menu and
the default background are first put back to how a fresh database has them, and the demo's own image
removed, so running the seed again tells the same story.
"""

import random
from collections.abc import Callable
from datetime import datetime, timedelta
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Max
from django.urls import reverse
from django.utils import timezone

from audit import trail
from audit.demo import HOUSE, IPV6, PHONES, Moment, ago
from residents.models import Resident, Role, RoleAssignment

from . import bin as cms_bin
from . import services
from .models import CmsImage, MenuItem, NewsItem, Page, PageRedirect, PageVersion, SiteSettings
from .models import Event as CmsEvent

# As cms migration 0006 seeds them: the menu and the default background of a fresh database.
MENU = [
    ("/", "Hjem"),
    ("/kollegielivet/historie", "Historie"),
    ("/vision", "Vision"),
    ("/kollegielivet", "Kollegielivet"),
    ("/faciliteter", "Faciliteter"),
    ("/begivenheder/", "Begivenheder"),
    ("/optagelse/", "Optagelse"),
    ("/legater", "Legater"),
    ("/kontakt", "Kontakt"),
]
DEFAULT_BACKGROUND = "/public/image/upload/images/gang.jpg"
IMAGE_CAPTION = "Gården en sommeraften"
BINNED_IMAGE_CAPTION = f"{IMAGE_CAPTION}, beskåret for skævt"
BINNED_PAGE = "Sommerfest 2026"

OPTAGELSE = (
    "<p>Sådan søger du ind.</p>\n"
    "<h2>Husleje</h2>\n"
    "<p>Huslejen er 3.150 kr. om måneden for et værelse med eget bad, alt inklusive.</p>\n"
    "<h2>Hvem kan bo her</h2>\n"
    "<p>Studerende ved en videregående uddannelse i København, med mindst to år tilbage.</p>\n"
    '<ul><li><a href="/optagelse/ansoeg">Anmod om rundvisning</a></li>'
    '<li><a href="/optagelse/fremlej">Ansøg om fremleje</a></li></ul>'
)
HISTORIE = (
    "<p>Kollegiets historie siden 1908.</p>\n"
    "<p>G. A. Hagemann gav kollegiet til studerende ved Polyteknisk Læreanstalt og "
    "Københavns Universitet.</p>\n"
    "<p>Bygningen på Hagemanns Gade blev tegnet af Ulrik Plesner og stod færdig i 1909.</p>"
)


def history(residents: list[Resident], now: datetime, rng: random.Random) -> list[Moment]:
    people = _people(now)
    if people is None:
        return []
    formand, admin, indstilling, inspektion = people
    _reset_shared_content()

    pages = {page.slug: page for page in Page.objects.all()}
    if not {"optagelse", "kollegielivet/historie", "faciliteter/kokken", "arkiv/gammel-side"} <= set(pages):
        return []
    optagelse, historie = pages["optagelse"], pages["kollegielivet/historie"]
    kokken, hidden = pages["faciliteter/kokken"], pages["arkiv/gammel-side"]
    oldest_news = NewsItem.objects.order_by("published_at", "pk").first()
    seeded_event = CmsEvent.objects.order_by("pk").first()
    today = timezone.localdate(now)
    news_at = ago(now, 33, 11, 40)
    christmas_at = ago(now, 21, 20, 5)

    def edit(at: datetime, by: Resident, page: Page, data: dict[str, str]) -> Moment:
        return Moment(
            at,
            _dated(at, lambda: services.save_page(Page.objects.get(pk=page.pk), data, by)),
            by,
            HOUSE,
            reverse("cms:page_edit", args=[page.pk]),
        )

    moments = [
        # News with a typo in its title; a shell fixes it two weeks later (System, outside the editor).
        Moment(
            news_at,
            lambda: services.save_news(
                None,
                {
                    "title": "Velkommen til efteråets nye alumner",
                    "body": "<p>Fem nye alumner flyttede ind i september. Sig hej i køkkenet!</p>",
                    "published_at": news_at,
                },
                formand,
            ),
            formand,
            HOUSE,
            reverse("cms:news_create"),
        ),
        Moment(ago(now, 19, 16, 30), _fix_news_title, None, None, source="system"),
        # Indstillingen writes the rent and the criteria into the Optagelse page (a line diff).
        edit(
            ago(now, 30, 19, 15),
            indstilling,
            optagelse,
            {"body": OPTAGELSE, "description": "Husleje, kriterier, rundvisning og fremleje."},
        ),
        edit(
            ago(now, 27, 10, 50),
            inspektion,
            kokken,
            {
                "body": "<p>Fælleskøkkenet på hver gang.</p>\n<p>Rengøring hver søndag efter tavlen i gangen.</p>"
            },
        ),
        # An image, its caption, and then the default background of every page without its own.
        Moment(ago(now, 25, 21, 3), _dated(ago(now, 25, 21, 3), lambda: _upload(admin)), admin, IPV6),
        Moment(
            ago(now, 25, 21, 9),
            lambda: services.update_image(
                CmsImage.objects.get(caption=IMAGE_CAPTION), f"{IMAGE_CAPTION} (Gården mod nord)", admin
            ),
            admin,
            IPV6,
        ),
        Moment(
            ago(now, 24, 9, 30),
            lambda: services.save_site_settings(
                {
                    "default_background": CmsImage.objects.get(
                        uploaded_by=admin, caption__startswith=IMAGE_CAPTION
                    ).url
                },
                formand,
            ),
            formand,
            HOUSE,
            reverse("cms:site_settings"),
        ),
        # A front-page event made, then moved a day and described by somebody else.
        Moment(
            christmas_at,
            lambda: services.save_event(
                None,
                {"title": "Julefrokost for alumner", "starts_on": (today + timedelta(days=20)).isoformat()},
                formand,
            ),
            formand,
            HOUSE,
            reverse("cms:event_create"),
        ),
        Moment(
            ago(now, 14, 17, 45),
            lambda: services.save_event(
                CmsEvent.objects.get(title="Julefrokost for alumner"),
                {
                    "starts_on": (today + timedelta(days=21)).isoformat(),
                    "description": "<p>Tilmelding i køkkenet senest en uge før.</p>",
                },
                indstilling,
            ),
            indstilling,
            HOUSE,
        ),
        # A page renamed: its old address becomes a redirect in the same change.
        edit(ago(now, 17, 13, 5), indstilling, hidden, {"slug": "arkiv/gamle-sider"}),
        # The menu reordered: one change, the whole list before and after.
        Moment(
            ago(now, 15, 20, 40),
            lambda: services.save_menu(_menu_with_optagelse_second(), formand),
            formand,
            HOUSE,
            reverse("cms:menu"),
        ),
        # Edited twice, then put back to the first edit: a restore, naming its version.
        edit(ago(now, 12, 15, 20), inspektion, historie, {"body": HISTORIE}),
        edit(
            ago(now, 10, 23, 58),
            admin,
            historie,
            {"body": "<p>Kollegiets historie siden 1908.</p>"},
        ),
        Moment(
            ago(now, 9, 8, 15),
            _dated(ago(now, 9, 8, 15), lambda: _restore(historie, inspektion, formand)),
            formand,
            HOUSE,
            reverse("cms:page_history", args=[historie.pk]),
        ),
        Moment(
            ago(now, 8, 18, 0),
            lambda: services.save_news(
                None,
                {
                    "title": "Rundvisninger i december",
                    "body": "<p>Vi viser kollegiet frem den første lørdag i december kl. 13.</p>",
                    "published_at": ago(now, 8, 18, 0),
                },
                indstilling,
            ),
            indstilling,
            PHONES[0],
            reverse("cms:news_create"),
        ),
        Moment(
            ago(now, 5, 12, 10),
            lambda: services.save_redirect(None, {"old_path": "rundvisning", "page": optagelse.pk}, formand),
            formand,
            HOUSE,
            reverse("cms:redirect_list"),
        ),
        Moment(
            ago(now, 3, 16, 25),
            _dated(
                ago(now, 3, 16, 25),
                lambda: services.save_page(
                    None,
                    {
                        "slug": "faciliteter/vaskeri",
                        "header": "Vaskeriet",
                        "body": "<p>Vaskeriet i kælderen er åbent hele døgnet.</p>",
                    },
                    inspektion,
                ),
            ),
            inspektion,
            HOUSE,
            reverse("cms:page_create"),
        ),
        # gahk.dk itself: published again by hand, and asked to update its code.
        Moment(
            ago(now, 2, 9, 5),
            _site_event("cms.site.published", formand),
            formand,
            HOUSE,
            reverse("cms:publish_now"),
        ),
        Moment(
            ago(now, 1, 22, 30),
            _site_event("cms.site.update_requested", admin),
            admin,
            IPV6,
            reverse("cms:update_now"),
        ),
        # Hours ago: what a page's editor names as its last save.
        edit(
            now - timedelta(hours=3),
            indstilling,
            optagelse,
            {"body": OPTAGELSE.replace("3.150 kr.", "3.250 kr.")},
        ),
    ]
    moments += _bin_moments(now, formand, admin, indstilling, inspektion)
    if oldest_news is not None:
        moments.append(
            Moment(
                ago(now, 6, 11, 0),
                _binned(ago(now, 6, 11, 0), lambda: cms_bin.put_in_bin(oldest_news, formand)),
                formand,
                HOUSE,
                reverse("cms:news_delete", args=[oldest_news.pk]),
            )
        )
    if seeded_event is not None:
        moments.append(
            Moment(
                ago(now, 4, 19, 35),
                _binned(ago(now, 4, 19, 35), lambda: cms_bin.put_in_bin(seeded_event, indstilling)),
                indstilling,
                HOUSE,
                reverse("cms:event_delete", args=[seeded_event.pk]),
            )
        )
    return moments


def _bin_moments(
    now: datetime, formand: Resident, admin: Resident, indstilling: Resident, inspektion: Resident
) -> list[Moment]:
    """What fills the bin besides the news item and the event: a page made, renamed and binned over a
    month ago (deletable for good now, by an administrator), and, days ago, a second picture, the
    rundvisning address and the Legater menu entry (restorable, not yet deletable)."""
    made_at, renamed_at, binned_at = ago(now, 35, 14, 0), ago(now, 34, 9, 30), ago(now, 32, 20, 15)
    upload_at = ago(now, 26, 18, 40)

    def page() -> Page:
        return Page.all_objects.get(header=BINNED_PAGE)

    return [
        Moment(
            made_at,
            _dated(
                made_at,
                lambda: services.save_page(
                    None,
                    {
                        "slug": "sommerfest",
                        "header": BINNED_PAGE,
                        "body": "<p>Sommerfesten holdes i gården lørdag den 20. juni. Alle alumner er velkomne.</p>",
                    },
                    indstilling,
                ),
            ),
            indstilling,
            HOUSE,
            reverse("cms:page_create"),
        ),
        Moment(
            renamed_at,
            _dated(renamed_at, lambda: services.save_page(page(), {"slug": "sommerfest-2026"}, indstilling)),
            indstilling,
            HOUSE,
        ),
        Moment(
            binned_at,
            _binned(binned_at, lambda: cms_bin.put_in_bin(page(), admin)),
            admin,
            IPV6,
        ),
        Moment(
            upload_at,
            _dated(upload_at, lambda: _upload(inspektion, BINNED_IMAGE_CAPTION, crop=True)),
            inspektion,
            HOUSE,
            reverse("cms:image_list"),
        ),
        Moment(
            ago(now, 3, 10, 20),
            _binned(
                ago(now, 3, 10, 20),
                lambda: cms_bin.put_in_bin(CmsImage.objects.get(caption=BINNED_IMAGE_CAPTION), inspektion),
            ),
            inspektion,
            HOUSE,
        ),
        Moment(
            ago(now, 2, 13, 50),
            _binned(
                ago(now, 2, 13, 50),
                lambda: cms_bin.put_in_bin(PageRedirect.objects.get(old_path="rundvisning"), formand),
            ),
            formand,
            HOUSE,
            reverse("cms:redirect_list"),
        ),
        Moment(
            ago(now, 7, 21, 0),
            _binned(
                ago(now, 7, 21, 0),
                lambda: services.save_menu(
                    [entry for entry in services.menu_state() if entry["url"] != "/legater"], formand
                ),
            ),
            formand,
            HOUSE,
            reverse("cms:menu"),
        ),
    ]


def _binned(at: datetime, play: Callable[[], object]) -> Callable[[], None]:
    """Run something that puts things in the bin, then date them to the event, as _dated does."""

    def run() -> None:
        started = timezone.now()
        play()
        for kind in cms_bin.KINDS.values():
            kind.model.all_objects.filter(binned_at__gte=started).update(binned_at=at)

    return run


def _people(now: datetime) -> tuple[Resident, Resident, Resident, Resident] | None:
    """formand@ and admin@, and a holder of each of two editor roles the seed hands out at random."""
    formand = Resident.objects.filter(email="formand@gahk.dk").first()
    admin = Resident.objects.filter(email="admin@gahk.dk").first()
    if formand is None or admin is None:
        return None
    today = timezone.localdate(now)

    def holder(role: str, besides: Resident | None = None) -> Resident:
        found = (
            RoleAssignment.objects.filter(role=role, year=today.year, month=today.month)
            .exclude(resident=besides)
            .select_related("resident")
            .order_by("pk")
            .first()
        )
        return found.resident if found else admin

    indstilling = holder(Role.INDSTILLING)
    return formand, admin, indstilling, holder(Role.INSPEKTION, besides=indstilling)


def _reset_shared_content() -> None:
    """What --fresh does not wipe, back to a fresh database's: the menu, the default background, and
    no demo image (a second run would add another)."""
    for image in CmsImage.all_objects.filter(caption__startswith=IMAGE_CAPTION):
        image.delete()  # its file too
    MenuItem.all_objects.all().delete()
    Page.all_objects.filter(header=BINNED_PAGE).delete()
    MenuItem.objects.bulk_create(
        [MenuItem(label=label, url=url, order=index * 10) for index, (url, label) in enumerate(MENU)]
    )
    settings_row = SiteSettings.load()
    settings_row.default_background = DEFAULT_BACKGROUND
    settings_row.save()


def _dated(at: datetime, play: Callable[[], object]) -> Callable[[], None]:
    """Run a service, then date what it stamped with the clock (versions, "last changed", upload
    time) to the event, as the trail is. Plain UPDATEs: the trail records none of these fields."""

    def run() -> None:
        newest = PageVersion.objects.aggregate(newest=Max("pk"))["newest"] or 0
        result = play()
        PageVersion.objects.filter(pk__gt=newest).update(created_at=at)
        if isinstance(result, Page):
            Page.objects.filter(pk=result.pk).update(updated_at=at)
            PageRedirect.objects.filter(page=result, created_at__gt=at).update(created_at=at)
        if isinstance(result, services.RestoreResult):
            Page.objects.filter(pk=result.page.pk).update(updated_at=at)
        if isinstance(result, CmsImage):
            CmsImage.objects.filter(pk=result.pk).update(uploaded_at=at)

    return run


def _upload(by: Resident, caption: str = IMAGE_CAPTION, *, crop: bool = False) -> CmsImage:
    from PIL import Image, ImageDraw

    picture = Image.new("RGB", (1600, 1000), "#2f5d50")
    draw = ImageDraw.Draw(picture)
    draw.rectangle((0, 620, 1600, 1000), fill="#6b8f3a")  # the lawn
    draw.rectangle((180, 160, 1420, 620), fill="#a4553a")  # the building
    for x in range(260, 1400, 160):
        draw.rectangle((x, 240, x + 70, 340), fill="#e8d9a8")
        draw.rectangle((x, 420, x + 70, 520), fill="#e8d9a8")
    if crop:
        picture = picture.rotate(4, fillcolor="#ffffff").crop((120, 0, 1500, 900))
    data = BytesIO()
    picture.save(data, format="JPEG", quality=85)
    upload = SimpleUploadedFile("gaarden-sommer.jpg", data.getvalue(), content_type="image/jpeg")
    return services.upload_image(upload, caption, by)


def _fix_news_title() -> None:
    """From a shell: no screen, so no named action; the log shows it as System, outside the editor."""
    item = NewsItem.objects.get(title="Velkommen til efteråets nye alumner")
    item.title = "Velkommen til efterårets nye alumner"
    item.save()


def _menu_with_optagelse_second() -> list[dict[str, str]]:
    menu = services.menu_state()
    optagelse = next((entry for entry in menu if entry["url"] == "/optagelse/"), None)
    if optagelse is None:
        return menu
    menu.remove(optagelse)
    menu.insert(1, optagelse)
    return menu


def _restore(page: Page, author: Resident, by: Resident) -> services.RestoreResult:
    version = PageVersion.objects.filter(page=page, created_by=author, note="Redigeret").latest(
        "created_at", "pk"
    )
    return services.restore_version(Page.objects.get(pk=page.pk), version, by)


def _site_event(action: str, by: Resident) -> Callable[[], None]:
    """The overview's two buttons, as events only: the real ones also queue a publish or ask the
    webhost to update its code, which a seed must not set off."""

    def play() -> None:
        with trail.event(action, actor=by) as event:
            event.involve_kind(*services.SITE, role="subject")

    return play
