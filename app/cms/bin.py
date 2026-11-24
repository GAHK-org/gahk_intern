"""The Hjemmeside's bin: nothing an editor made is deleted outright.

"Slet" on a page, a news item, a front-page event, an image, an old address or a menu item puts it
here (`put_in_bin`): it leaves gahk.dk and the editor's lists at the next publish, but the row, its
text and its file stay. From the bin it can be put back (`restore`) at any time, and deleted for good
(`delete_for_good`) only once it has been there longer than KEEP_FOR, one item at a time. Nothing
empties the bin by itself.

These are writes like cms.services', and follow its rules: one audit event each, run in a
transaction, and gahk.dk published after the commit. Permissions are the caller's business, except
that the waiting time is enforced here, so no view can forget it.

A page takes its old addresses (PageRedirect) with it: they are binned at the same moment, come back
with it, and go for good with it. They are not listed in the bin on their own while their page is in
it, since they have nowhere to send anyone.
"""

import datetime
from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from audit import trail
from residents.models import Resident

from .models import FRONT_PAGE_ID, Binnable, CmsImage, Event, MenuItem, NewsItem, Page, PageRedirect
from .services import MAX_MENU_ITEMS, menu_state, plain_text, publish_on_commit, usage_map
from .snapshot import media_names

# How long an item stays in the bin before it may be deleted for good. "More than": an item binned
# at 10:00 on 1 November can go from 10:00 on 1 December.
KEEP_FOR = datetime.timedelta(days=30)


@dataclass(frozen=True)
class BinKind:
    key: str  # in the bin's URLs and anchors
    model: type[Binnable]
    trail_kind: str  # its audit kind ("cms.page"), which names its events
    label: str  # Danish, as the bin lists it
    the: str  # "siden", for the audit actions' labels
    # Whether putting it in the bin and deleting it for good are for administrators only, as deleting
    # a page always was.
    administrators_only: bool = False


KINDS: dict[str, BinKind] = {
    kind.key: kind
    for kind in (
        BinKind("page", Page, "cms.page", "Side", "siden", administrators_only=True),
        BinKind("news", NewsItem, "cms.news", "Nyhed", "nyheden"),
        BinKind("event", Event, "cms.event", "Forsidebegivenhed", "forsidebegivenheden"),
        BinKind("image", CmsImage, "cms.image", "Billede", "billedet"),
        BinKind("redirect", PageRedirect, "cms.redirect", "Omdirigering", "omdirigeringen"),
        BinKind("menu_item", MenuItem, "cms.menu_item", "Menupunkt", "menupunktet"),
    )
}


def kind_of(obj: Binnable) -> BinKind:
    return next(kind for kind in KINDS.values() if isinstance(obj, kind.model))


def action_labels() -> dict[str, str]:
    """The audit trail's Danish names for the bin's events, for cms.auditing."""
    labels = {}
    for kind in KINDS.values():
        labels[f"{kind.trail_kind}.binned"] = f"Lagde {kind.the} i papirkurven"
        labels[f"{kind.trail_kind}.restored_from_bin"] = f"Gendannede {kind.the} fra papirkurven"
        labels[f"{kind.trail_kind}.deleted"] = f"Slettede {kind.the} permanent"
    return labels


def deletable_from(obj: Binnable) -> datetime.datetime | None:
    """When `obj` may be deleted for good (None while it is not in the bin)."""
    return obj.binned_at + KEEP_FOR if obj.binned_at else None


def may_delete_now(obj: Binnable, now: datetime.datetime | None = None) -> bool:
    deletable = deletable_from(obj)
    return deletable is not None and (now or timezone.now()) >= deletable


def _locked(obj: Binnable) -> Any:  # noqa: ANN401 - the same model as obj
    """`obj` read again under a row lock, binned or not, so two clicks cannot both act on it."""
    return type(obj).all_objects.select_for_update().get(pk=obj.pk)


# ---------------------------------------------------------------------------------- put in the bin
def put_in_bin(obj: Binnable, by: Resident | None) -> None:
    """Move `obj` to the bin. Raises ValidationError (Danish) for what may not go: the front page, an
    image something still shows."""
    kind = kind_of(obj)
    if isinstance(obj, Page) and obj.pk == FRONT_PAGE_ID:
        raise ValidationError("Forsiden kan ikke slettes.")
    if isinstance(obj, CmsImage) and (usage := usage_map([obj])[obj.pk]):
        raise ValidationError(f"Billedet bruges stadig ({', '.join(usage)}), så det kan ikke slettes.")
    with trail.event(f"{kind.trail_kind}.binned", actor=by, subject=obj) as event:
        obj = _locked(obj)
        if obj.binned_at is not None:
            event.cancel()
            return
        now = timezone.now()
        if isinstance(obj, MenuItem):
            _bin_menu_item(obj, by, now, event)
        else:
            _mark_binned(obj, by, now)
        if isinstance(obj, Page):
            for redirect in PageRedirect.objects.filter(page=obj):
                _mark_binned(redirect, by, now)
        publish_on_commit("save", by)


def _mark_binned(obj: Binnable, by: Resident | None, at: datetime.datetime) -> None:
    obj.binned_at = at
    obj.binned_by = by
    obj.save(update_fields=["binned_at", "binned_by"])


def _bin_menu_item(item: MenuItem, by: Resident | None, at: datetime.datetime, event: trail.Event) -> None:
    """The menu is one thing to the editor (cms.services.save_menu): one change, the list before and
    after."""
    before = menu_state()
    with trail.unaudited():
        _mark_binned(item, by, at)
    event.change_kind("cms.menu", None, "Menu", "menu", before, menu_state())


# ------------------------------------------------------------------------------------------ restore
def restore(obj: Binnable, by: Resident | None) -> list[str]:
    """Put `obj` back where it was. Returns warnings (Danish) about what could not come back with it;
    raises ValidationError when it cannot come back at all."""
    kind = kind_of(obj)
    warnings: list[str] = []
    with trail.event(f"{kind.trail_kind}.restored_from_bin", actor=by, subject=obj) as event:
        obj = _locked(obj)
        if obj.binned_at is None:
            event.cancel()
            return []
        if isinstance(obj, Page):
            _restore_page(obj, warnings)
        elif isinstance(obj, PageRedirect):
            _check_redirect_restorable(obj)
            _mark_restored(obj)
        elif isinstance(obj, MenuItem):
            _restore_menu_item(obj, event)
        else:
            _mark_restored(obj)
        if isinstance(obj, Page | NewsItem | Event):
            warnings += _binned_images_warning(obj)
        publish_on_commit("save", by)
    return warnings


def _mark_restored(obj: Binnable) -> None:
    obj.binned_at = None
    obj.binned_by = None
    obj.save(update_fields=["binned_at", "binned_by"])


def _restore_page(page: Page, warnings: list[str]) -> None:
    binned_at = page.binned_at
    if page.slug and Page.objects.filter(slug=page.slug).exists():
        warnings.append(
            f"Adressen /{page.slug} bruges nu af en anden side, så siden er gendannet uden adresse "
            "og vises ikke på gahk.dk, før du giver den en ny."
        )
        page.slug = None
        page.binned_at = None
        page.binned_by = None
        page.save(update_fields=["slug", "binned_at", "binned_by"])
    else:
        _mark_restored(page)
    # Its own old addresses, binned with it — not one binned by itself before (that stays in the bin).
    for redirect in PageRedirect.all_objects.filter(page=page, binned_at=binned_at).order_by("old_path"):
        try:
            _check_redirect_restorable(redirect)
        except ValidationError as exc:
            warnings.append(f"Den gamle adresse /{redirect.old_path} blev i papirkurven: {exc.messages[0]}")
        else:
            _mark_restored(redirect)


def _check_redirect_restorable(redirect: PageRedirect) -> None:
    page = redirect.page  # through the base manager, so a binned page too
    if page.binned_at is not None:
        raise ValidationError(f"Siden “{page.header}” ligger i papirkurven. Gendan siden først.")
    if clash := Page.objects.filter(slug=redirect.old_path).first():
        raise ValidationError(f"/{redirect.old_path} er nu adressen på siden “{clash.header}”.")
    if PageRedirect.objects.filter(old_path=redirect.old_path).exists():
        raise ValidationError(f"/{redirect.old_path} sender nu videre til en anden side.")


def _restore_menu_item(item: MenuItem, event: trail.Event) -> None:
    """Back at the end of the menu: where it stood may mean something else by now."""
    if MenuItem.objects.count() >= MAX_MENU_ITEMS:
        raise ValidationError(f"Menuen har allerede {MAX_MENU_ITEMS} punkter, så der er ikke plads.")
    before = menu_state()
    last = MenuItem.objects.aggregate(last=models.Max("order"))["last"]
    with trail.unaudited():
        item.binned_at = None
        item.binned_by = None
        item.order = 0 if last is None else last + 10
        item.save(update_fields=["binned_at", "binned_by", "order"])
    event.change_kind("cms.menu", None, "Menu", "menu", before, menu_state())


def _texts(obj: Binnable) -> list[str]:
    if isinstance(obj, Page):
        return [obj.body, obj.background_image]
    if isinstance(obj, NewsItem):
        return [obj.body]
    if isinstance(obj, Event):
        return [obj.description]
    return []


def _binned_images_warning(obj: Binnable) -> list[str]:
    """A warning naming the binned images that restored content shows (they show as missing until
    they are restored too)."""
    texts = _texts(obj)
    names = set().union(*(media_names(text) for text in texts)) if texts else set()
    binned = [
        image
        for image in CmsImage.all_objects.filter(binned_at__isnull=False)
        if image.file and (image.file.name in names or any(image.url in text for text in texts))
    ]
    if not binned:
        return []
    listed = ", ".join(f"“{image}”" for image in binned)
    return [
        f"Den bruger billeder, der ligger i papirkurven, og som ikke vises, før de også gendannes: {listed}."
    ]


# -------------------------------------------------------------------------------- delete for good
def delete_for_good(obj: Binnable, by: Resident | None, now: datetime.datetime | None = None) -> None:
    """Delete a binned item permanently. Raises ValidationError before KEEP_FOR has passed, for an
    item not in the bin, and for an image that content (in the bin or not) still shows."""
    kind = kind_of(obj)
    with trail.event(f"{kind.trail_kind}.deleted", actor=by, subject=obj):
        obj = _locked(obj)
        if obj.binned_at is None:
            raise ValidationError("Kun noget i papirkurven kan slettes permanent.")
        if not may_delete_now(obj, now):
            when = timezone.localtime(deletable_from(obj)).strftime("%d-%m-%Y kl. %H:%M")
            raise ValidationError(f"Det kan først slettes permanent fra {when}.")
        if isinstance(obj, CmsImage) and (usage := image_usage_everywhere(obj)):
            raise ValidationError(
                f"Billedet bruges stadig ({', '.join(usage)}), så det kan ikke slettes permanent."
            )
        # A page's versions stay (PageVersion.page is SET_NULL); its old addresses go with it.
        obj.delete()
        publish_on_commit("save", by)


def image_usage_everywhere(image: CmsImage) -> list[str]:
    """Where content shows the image, the bin's content included (marked as such)."""
    return usage_map([image], include_binned=True)[image.pk]


# ------------------------------------------------------------------------------------- the listing
@dataclass
class BinEntry:
    kind: BinKind
    obj: Binnable
    name: str
    detail: str = ""  # an address, a link, the start of a text
    redirects: list[str] = field(default_factory=list)  # a page's old addresses, binned with it
    usage: list[str] = field(default_factory=list)  # an image: what still shows it

    @property
    def detail_is_path(self) -> bool:
        return self.kind.key in ("page", "menu_item") and self.detail.startswith("/")

    @property
    def anchor(self) -> str:
        return f"{self.kind.key}-{self.obj.pk}"

    @property
    def deletable_from(self) -> datetime.datetime | None:
        return deletable_from(self.obj)

    @property
    def may_delete_now(self) -> bool:
        return may_delete_now(self.obj)


def _excerpt(html: str, length: int = 160) -> str:
    from django.utils.html import strip_tags

    text = plain_text(strip_tags(html))
    return text if len(text) <= length else text[: length - 1].rstrip() + "…"


def entries() -> list[BinEntry]:
    """Everything in the bin, most recently binned first."""
    found: list[BinEntry] = []
    for page in Page.all_objects.filter(binned_at__isnull=False).select_related("binned_by"):
        redirects = PageRedirect.all_objects.filter(page=page, binned_at=page.binned_at)
        found.append(
            BinEntry(
                KINDS["page"],
                page,
                page.header,
                f"/{page.slug}" if page.slug else "Uden adresse",
                redirects=[f"/{r.old_path}" for r in redirects.order_by("old_path")],
            )
        )
    for item in NewsItem.all_objects.filter(binned_at__isnull=False).select_related("binned_by"):
        found.append(BinEntry(KINDS["news"], item, item.title, _excerpt(item.body)))
    for event in Event.all_objects.filter(binned_at__isnull=False).select_related("binned_by"):
        detail = f"{event.starts_on:%d-%m-%Y}. {_excerpt(event.description)}".strip(". ")
        found.append(BinEntry(KINDS["event"], event, event.title, detail))
    images = list(CmsImage.all_objects.filter(binned_at__isnull=False).select_related("binned_by"))
    usage = usage_map(images, include_binned=True)
    for image in images:
        found.append(BinEntry(KINDS["image"], image, str(image), usage=usage[image.pk]))
    redirects = (
        PageRedirect.all_objects.filter(binned_at__isnull=False, page__binned_at__isnull=True)
        .select_related("binned_by", "page")
        .order_by("old_path")
    )
    for redirect in redirects:
        found.append(
            BinEntry(KINDS["redirect"], redirect, f"/{redirect.old_path}", f"til “{redirect.page.header}”")
        )
    for entry in MenuItem.all_objects.filter(binned_at__isnull=False).select_related("binned_by"):
        found.append(BinEntry(KINDS["menu_item"], entry, entry.label, entry.url))
    found.sort(key=lambda entry: (entry.obj.binned_at, entry.obj.pk), reverse=True)
    return found


def count() -> int:
    """How many items the bin lists (a page's old addresses count with the page)."""
    return (
        sum(kind.model.all_objects.filter(binned_at__isnull=False).count() for kind in KINDS.values())
        - PageRedirect.all_objects.filter(page__binned_at__isnull=False).count()
    )


def find(kind_key: str, pk: int) -> Binnable | None:
    """The binned item of that kind and id, or None."""
    kind = KINDS.get(kind_key)
    if kind is None:
        return None
    return kind.model.all_objects.filter(pk=pk, binned_at__isnull=False).first()
