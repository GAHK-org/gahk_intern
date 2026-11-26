"""Drive files and folders embedded in a post on opslagstavlen (drive.embeds, opslagstavle.embeds):
drawn for each reader by Drive's access rules, a warning on what not every resident may open, the
composer's picker, and "Del på opslagstavlen" on a file's and a folder's page."""

import re
from collections.abc import Callable
from uuid import uuid4

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core import embeds as embed_syntax
from core.models import Workgroup
from drive import shared
from drive.access import Viewer
from drive.models import File, Folder, SharedFolder, Space
from drive.structure import ensure_structure
from opslagstavle.models import Notice
from residents.models import Resident

from .drive_support import (  # noqa: F401 - fixtures, used by name
    TODAY,
    WINTER,
    _period_folder,
    _today,
    _upload,
    group,
    open_gate,
    resident_in,
    room,
)
from .test_opslagstavle_embeds import BOARD, body_of, textarea

pytestmark = pytest.mark.django_db

CLOSED_FILE = "Du har ikke adgang til denne fil."
NOT_EVERYONE_FILE = "Ikke alle kan åbne denne fil."


@pytest.fixture
def people(resident_in: Callable[..., Resident], group: Workgroup) -> dict[str, Resident]:
    found = {
        "member": resident_in({(2026, 11): group}),
        "outsider": resident_in({(2026, 11): None}),
    }
    ensure_structure(TODAY)
    return found


def client_for(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def this_year() -> Folder:
    return Folder.objects.get(kind=Folder.Kind.YEAR, period_start=TODAY.replace(month=1, day=1))


def put(resident: Resident, folder: Folder, name: str, content: bytes = b"x") -> File:
    assert _upload(client_for(resident), folder, name, content)["status"] == 201
    return File.objects.get(folder=folder, name=name, deleted_at__isnull=True)


def subfolder(parent: Folder, name: str) -> Folder:
    folder = Folder(
        parent=parent,
        name=name,
        space=parent.space,
        workgroup=parent.workgroup,
        period_start=parent.period_start,
        shared=parent.shared,
    )
    folder.save()
    return folder


def embed(item: File | Folder) -> str:
    return embed_syntax.markdown("drive", item.uuid, item.name)


def post(author: Resident, body: str) -> Notice:
    return Notice.objects.create(author=author, body=body)


def widgets(html: str) -> list[str]:
    return re.findall(r'<div class="drive-embed.*?\n</div>', html, re.DOTALL)


def seen_by(resident: Resident, notice: Notice) -> str:
    """The one Drive widget on the post's own page, as `resident` sees it."""
    [widget] = widgets(body_of(client_for(resident).get(f"{BOARD}{notice.pk}").content.decode()))
    return widget


# --- the syntax -----------------------------------------------------------------------------------


def test_a_drive_embed_is_keyed_by_its_uuid_and_nothing_else() -> None:
    key = uuid4()

    assert embed_syntax.parse(f"drive:{key}") == embed_syntax.Embed("drive", key)
    assert embed_syntax.parse(f"drive:{str(key).upper()}") == embed_syntax.Embed("drive", key)
    # A counting number is no Drive key, and a uuid is no album's.
    assert embed_syntax.parse("drive:12") is None
    assert embed_syntax.parse(f"album:{key}") is None
    assert embed_syntax.markdown("drive", key, "Budget.xlsx") == f"![Budget.xlsx](drive:{key})"


# --- what every reader sees -----------------------------------------------------------------------


def test_a_file_every_resident_may_open_is_drawn_with_no_warning(
    people: dict[str, Resident], open_gate: None
) -> None:
    file = put(people["member"], this_year(), "Husmøde referat.pdf", b"%PDF-1.4")
    notice = post(people["member"], f"Referatet fra husmødet:\n\n{embed(file)}")

    widget = seen_by(people["outsider"], notice)

    assert f'href="{reverse("drive:file", args=[file.uuid])}"' in widget
    assert f'href="{reverse("drive:download", args=[file.uuid])}"' in widget
    assert "Drive · Fælles / 2026" in widget
    assert "<strong>Husmøde referat.pdf</strong>" in widget
    assert "#i-doc" in widget
    assert "drive-embed-warning" not in widget


def test_a_groups_file_warns_its_readers_and_is_closed_to_everyone_else(
    people: dict[str, Resident], group: Workgroup, open_gate: None
) -> None:
    file = put(people["member"], _period_folder(group, WINTER), "Budget.xlsx", b"budget")
    notice = post(people["member"], embed(file))

    member = seen_by(people["member"], notice)
    outsider = seen_by(people["outsider"], notice)

    assert f'href="{reverse("drive:file", args=[file.uuid])}"' in member
    assert NOT_EVERYONE_FILE in member
    assert f"Den ligger hos {group.name} i Drive" in member

    # The widget, its name and why it is closed: nothing of where it lives, how big it is, or a link.
    assert "<strong>Budget.xlsx</strong>" in outsider
    assert CLOSED_FILE in outsider
    assert f"Den ligger hos {group.name} i Drive" in outsider
    assert str(file.uuid) not in outsider
    assert "<a " not in outsider
    assert "2026-27 Vinter" not in outsider
    assert "bytes" not in outsider


def test_the_kitchen_account_sees_it_closed(
    people: dict[str, Resident], make_resident: Callable[..., Resident], open_gate: None
) -> None:
    """Drive is closed to it, and the board tells it so rather than showing a bare name. It sees only
    its own posts, so it is the author here."""
    file = put(people["member"], this_year(), "Plakat.pdf")
    kitchen = make_resident(email="koekken@gahk.dk", is_kitchen=True)
    notice = post(kitchen, embed(file))

    widget = seen_by(kitchen, notice)

    assert CLOSED_FILE in widget
    assert "<a " not in widget


def test_while_drive_is_on_trial_everything_warns_and_is_closed_outside_the_trial(
    people: dict[str, Resident], make_resident: Callable[..., Resident]
) -> None:
    from residents.models import Role

    admin = make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,))
    file = put(admin, this_year(), "Husorden.md", b"# Husorden")
    notice = post(admin, embed(file))

    inside = seen_by(admin, notice)
    outside = seen_by(people["outsider"], notice)

    assert NOT_EVERYONE_FILE in inside
    assert "kun åbent for administratorer og Inspektionen" in inside
    assert CLOSED_FILE in outside
    assert "kun åbent for administratorer og Inspektionen" in outside


def test_a_folder_shows_its_first_files_and_counts_them_all_the_way_down(
    people: dict[str, Resident], open_gate: None
) -> None:
    party = subfolder(this_year(), "Julefrokost")
    songs = subfolder(party, "Sange")
    put(people["member"], songs, "Fællessang.pdf", b"12345")
    for n in range(6):
        put(people["member"], party, f"Bilag {n}.pdf", b"123")
    notice = post(people["member"], embed(party))

    widget = seen_by(people["outsider"], notice)

    assert f'href="{reverse("drive:folder", args=[party.uuid])}"' in widget
    assert "Drive · Fælles / 2026" in widget
    assert "7 filer" in widget
    # The subfolder first, then the files by name, five in all.
    entries = re.findall(r"<span>([^<]+)</span></a></li>", widget)
    assert entries == ["Sange", "Bilag 0.pdf", "Bilag 1.pdf", "Bilag 2.pdf", "Bilag 3.pdf"]
    assert "og 2 mere" in widget
    assert "drive-embed-warning" not in widget


def test_a_folder_lists_only_the_subfolders_its_reader_may_open(
    people: dict[str, Resident], group: Workgroup, open_gate: None
) -> None:
    groups_root = Folder.objects.get(kind=Folder.Kind.GROUPS_ROOT)
    notice = post(people["member"], embed(groups_root))

    member = seen_by(people["member"], notice)
    outsider = seen_by(people["outsider"], notice)

    assert f"<span>{group.name}</span>" in member
    assert group.name not in outsider
    assert "Mappen er tom." in outsider
    # Its count would take in every group's files, also those the reader cannot see.
    assert "drive-embed-meta" not in member


def test_a_delt_mappe_warns_until_it_is_archived(
    people: dict[str, Resident], resident_in: Callable[..., Resident], open_gate: None
) -> None:
    others = [people["outsider"], resident_in({(2026, 11): None})]
    folder = shared.create(
        Viewer.for_resident(people["member"]),
        name="Temafest",
        members=[resident.pk for resident in others],
        expires_on=TODAY.replace(year=2027),
        on_expiry=SharedFolder.OnExpiry.ARCHIVE,
        actor=people["member"],
    )
    file = put(people["member"], folder, "Program.pdf")
    notice = post(people["member"], embed(file))

    assert "Den ligger i en delt mappe, så kun dens medlemmer kan åbne den." in seen_by(
        people["member"], notice
    )

    SharedFolder.objects.filter(pk=folder.shared_id).update(archived_at=timezone.now())

    assert "drive-embed-warning" not in seen_by(people["member"], notice)


def test_a_file_in_the_bin_or_gone_leaves_its_name(people: dict[str, Resident], open_gate: None) -> None:
    file = put(people["member"], this_year(), "Gammel plan.pdf")
    File.objects.filter(pk=file.pk).update(deleted_at=timezone.now())
    notice = post(people["member"], f"{embed(file)}\n\n![Aldrig set.pdf](drive:{uuid4()})")

    text = body_of(client_for(people["member"]).get(f"{BOARD}{notice.pk}").content.decode())

    assert widgets(text) == []
    assert "<p>Gammel plan.pdf</p>" in text
    assert "<p>Aldrig set.pdf</p>" in text


def test_the_feed_shows_the_widget_under_the_excerpt(people: dict[str, Resident], open_gate: None) -> None:
    file = put(people["member"], this_year(), "Husorden.pdf")
    post(people["member"], f"Læs den nye husorden\n\n{embed(file)}")

    html = client_for(people["outsider"]).get(BOARD).content.decode()

    assert len(widgets(html)) == 1


def test_drive_widgets_cost_the_same_queries_however_many_posts_embed_one(
    people: dict[str, Resident], group: Workgroup, open_gate: None
) -> None:
    member = people["member"]
    notices = [post(member, "Ingen indlejringer") for _ in range(4)]
    client = client_for(member)

    def embedding(notice: Notice, n: int) -> None:
        folder = subfolder(this_year(), f"Mappe {n}")
        put(member, folder, f"Indhold {n}.pdf")
        image = put(member, _period_folder(group, WINTER), f"Billede {n}.png")
        notice.body = f"{embed(folder)}\n\n{embed(image)}"
        notice.save()

    def board_queries() -> int:
        with CaptureQueriesContext(connection) as queries:
            assert client.get(BOARD).status_code == 200
        return len(queries.captured_queries)

    embedding(notices[0], 0)
    one_embedding = board_queries()
    for n, notice in enumerate(notices[1:], start=1):
        embedding(notice, n)

    assert board_queries() == one_embedding


# --- the composer ---------------------------------------------------------------------------------


def test_the_composer_warns_before_posting(
    people: dict[str, Resident], group: Workgroup, open_gate: None
) -> None:
    file = put(people["member"], _period_folder(group, WINTER), "Budget.xlsx")
    client = client_for(people["member"])

    drawn = client.post(BOARD + "indlejret", {"body": f"Hej\n\n{embed(file)}"}).content.decode()

    [widget] = widgets(drawn)
    assert NOT_EVERYONE_FILE in widget
    assert 'target="_blank"' in widget
    assert f'data-embed-remove="drive:{file.uuid}"' in drawn


def test_the_composer_offers_drive_to_those_who_may_use_it(
    people: dict[str, Resident], make_resident: Callable[..., Resident]
) -> None:
    from residents.models import Role

    admin = make_resident(email="admin@gahk.dk", roles=(Role.ADMINISTRATOR,))

    assert 'data-embed-open="drive"' in client_for(admin).get(BOARD + "opret").content.decode()
    plain = client_for(people["outsider"]).get(BOARD + "opret").content.decode()
    assert 'data-embed-open="drive"' not in plain
    assert 'id="js-embed-drive"' not in plain
    assert (
        client_for(people["outsider"]).get(BOARD + "indlejr/drive").content.decode().count("data-embed-pick")
        == 0
    )


def test_the_drive_picker_lists_recent_files_and_finds_by_name(
    people: dict[str, Resident], group: Workgroup, open_gate: None
) -> None:
    member, outsider = people["member"], people["outsider"]
    put(member, this_year(), "Husorden.pdf")
    secret = put(member, _period_folder(group, WINTER), "Budget.xlsx")
    budgets = subfolder(this_year(), "Budgetter")

    newest = client_for(member).get(BOARD + "indlejr/drive").content.decode()
    found = client_for(member).get(BOARD + "indlejr/drive", {"q": "budget"}).content.decode()
    theirs = client_for(outsider).get(BOARD + "indlejr/drive", {"q": "budget"}).content.decode()

    picks = re.findall(r'data-embed="([^"]+)"', newest)
    assert picks[0] == embed(secret)  # the most recently changed first
    assert newest.count("Ikke alle kan åbne den") == 1
    assert "Fælles / 2026" in newest
    # Folders first, then files.
    assert re.findall(r'data-embed="([^"]+)"', found) == [embed(budgets), embed(secret)]
    assert "Budget.xlsx" not in theirs
    assert "Budgetter" in theirs


def test_sharing_a_file_starts_a_post_with_it_embedded(
    people: dict[str, Resident], group: Workgroup, open_gate: None
) -> None:
    file = put(people["member"], _period_folder(group, WINTER), "Budget.xlsx")
    client = client_for(people["member"])

    page = client.get(reverse("drive:file", args=[file.uuid])).content.decode()
    share = f"{BOARD}opret?drive={file.uuid}"
    assert f'href="{share}"' in page

    html = client.get(share).content.decode()
    assert textarea(html)[1].strip("\n") == embed(file)
    assert NOT_EVERYONE_FILE in html

    # Not by someone who cannot read it.
    assert textarea(client_for(people["outsider"]).get(share).content.decode())[1].strip() == ""


def test_sharing_a_folder_starts_a_post_with_it_embedded(
    people: dict[str, Resident], open_gate: None
) -> None:
    folder = subfolder(this_year(), "Julefrokost")
    client = client_for(people["member"])

    page = client.get(reverse("drive:folder", args=[folder.uuid])).content.decode()
    assert f'href="{BOARD}opret?drive={folder.uuid}"' in page
    html = client.get(f"{BOARD}opret?drive={folder.uuid}").content.decode()
    assert textarea(html)[1].strip("\n") == embed(folder)


def test_everyone_is_the_access_table_for_a_resident_with_nothing_of_their_own(
    people: dict[str, Resident], group: Workgroup, open_gate: None
) -> None:
    """Fælles is for everyone, a group's folders and Embedsgrupper' own groups are not."""
    from drive.embeds import _Everyone

    folders = [
        this_year(),
        Folder.objects.get(kind=Folder.Kind.TIMELESS, space=Space.COMMON),
        Folder.objects.get(kind=Folder.Kind.GROUPS_ROOT),
        Folder.objects.get(kind=Folder.Kind.GROUP, workgroup=group),
        _period_folder(group, WINTER),
    ]
    everyone = _Everyone(folders)

    assert [everyone.warning(folder) == "" for folder in folders] == [True, True, True, False, False]
