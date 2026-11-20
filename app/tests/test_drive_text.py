"""Drive: `Ny fil` and the text editor (drive/text.py)."""

from collections.abc import Callable

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import Workgroup
from drive import services
from drive.models import File
from drive.structure import ensure_structure
from residents.models import Resident

from .drive_support import (  # noqa: F401 - fixtures, used by name
    WINTER,
    _period_folder,
    _today,
    _upload,
    group,
    open_gate,
    resident_in,
    room,
)


@pytest.fixture
def people(resident_in: Callable[..., Resident], group: Workgroup, open_gate: None) -> dict[str, Resident]:
    found = {
        "member": resident_in({(2026, 11): group}),
        "other": resident_in({(2026, 11): group}),
        "outsider": resident_in({(2026, 11): None}),
    }
    ensure_structure()
    return found


def _client(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def _bytes(file: File) -> bytes:
    with services.storage().open(file.latest_version().key, "rb") as source:
        return source.read()


def _edit(client: Client, file: File, content: str, base: int | None = None) -> object:
    number = file.latest_version().number if base is None else base
    return client.post(reverse("drive:edit_text", args=[file.pk]), {"content": content, "base": number})


def test_ny_fil_makes_a_text_file_and_opens_it_in_the_editor(
    people: dict[str, Resident], group: Workgroup
) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    folder = member.get(reverse("drive:folder", args=[winter.pk])).content.decode()
    assert "data-drive-new-file-open" in folder
    # Live editing is off in the tests: its kinds are there, greyed out, with a word on why.
    assert 'value="document" class="drive-new-file-kind" disabled' in folder
    assert "er slået fra" in folder

    response = member.post(
        reverse("drive:new_file", args=[winter.pk]), {"kind": "markdown", "name": "Husregler"}
    )
    made = File.objects.get(folder=winter, name="Husregler.md")
    assert response["Location"] == reverse("drive:edit_text", args=[made.pk])
    assert _bytes(made) == b""
    member.post(reverse("drive:new_file", args=[winter.pk]), {"kind": "text"})
    member.post(reverse("drive:new_file", args=[winter.pk]), {"kind": "text", "name": "Ny tekstfil.txt"})
    assert set(File.objects.filter(folder=winter).values_list("name", flat=True)) == {
        "Husregler.md",
        "Ny tekstfil.txt",
        "Ny tekstfil (2).txt",
    }

    # An office kind while live editing is off makes nothing.
    member.post(reverse("drive:new_file", args=[winter.pk]), {"kind": "document"})
    assert not File.objects.filter(name__endswith=".docx").exists()
    # Nor anything for someone who may not put files there.
    outsider = _client(people["outsider"])
    assert outsider.post(reverse("drive:new_file", args=[winter.pk]), {"kind": "text"}).status_code == 404


def test_saving_makes_the_next_version(people: dict[str, Resident], group: Workgroup) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    _upload(member, winter, "Noter.md", b"# Noter\n")
    file = File.objects.get(name="Noter.md")
    page = member.get(reverse("drive:file", args=[file.pk])).content.decode()
    assert reverse("drive:edit_text", args=[file.pk]) in page
    editor = member.get(reverse("drive:edit_text", args=[file.pk])).content.decode()
    assert "# Noter\n</textarea>" in editor
    assert "Markdown:" in editor

    response = _edit(member, file, "# Noter\r\n\r\nHusk **mødet**.\r\n")
    assert response["Location"] == reverse("drive:file", args=[file.pk])  # type: ignore[index]
    file.refresh_from_db()
    assert file.latest_version().number == 2
    assert _bytes(file) == "# Noter\n\nHusk **mødet**.\n".encode()
    assert file.size == len(_bytes(file))

    _edit(member, file, "# Noter\n\nHusk **mødet**.\n")  # nothing changed: no version
    assert file.versions.count() == 2


def test_a_save_after_somebody_elses_is_refused_and_the_text_stays(
    people: dict[str, Resident], group: Workgroup
) -> None:
    member, other = _client(people["member"]), _client(people["other"])
    _upload(member, _period_folder(group, WINTER), "Liste.txt", b"mel\n")
    file = File.objects.get(name="Liste.txt")
    opened_at = file.latest_version().number
    _edit(other, file, "mel\nsukker\n", base=opened_at)

    response = _edit(member, file, "mel\nsmør\n", base=opened_at)
    page = response.content.decode()  # type: ignore[attr-defined]
    assert "gemt af en anden" in page
    assert "mel\nsmør\n</textarea>" in page
    assert _bytes(file) == b"mel\nsukker\n"
    assert file.versions.count() == 2
    # Saving again from there, having been told, goes through.
    assert _edit(member, file, "mel\nsmør\n", base=2)["Location"] == reverse(  # type: ignore[index]
        "drive:file", args=[file.pk]
    )
    assert _bytes(file) == "mel\nsmør\n".encode()


def test_a_csv_keeps_excels_mark_and_windows_line_endings(
    people: dict[str, Resident], group: Workgroup
) -> None:
    member = _client(people["member"])
    _upload(member, _period_folder(group, WINTER), "Kasse.csv", "Vare;Pris\r\nØl;12\r\n".encode("cp1252"))
    file = File.objects.get(name="Kasse.csv")
    assert (
        "Vare;Pris\nØl;12\n</textarea>"
        in member.get(reverse("drive:edit_text", args=[file.pk])).content.decode()
    )
    _edit(member, file, "Vare;Pris\nØl;12\nVand;5\n")
    assert _bytes(file) == "﻿Vare;Pris\r\nØl;12\r\nVand;5\r\n".encode()


def test_only_text_files_that_can_be_changed_open_in_the_editor(
    people: dict[str, Resident], group: Workgroup
) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    _upload(member, winter, "Billede.png", b"x")
    _upload(member, winter, "Låst.txt", b"x")
    picture = File.objects.get(name="Billede.png")
    assert member.get(reverse("drive:edit_text", args=[picture.pk])).status_code == 404

    locked = File.objects.get(name="Låst.txt")
    File.objects.filter(pk=locked.pk).update(locked_at=timezone.now())
    assert member.get(reverse("drive:edit_text", args=[locked.pk])).status_code == 403
    assert (
        reverse("drive:edit_text", args=[locked.pk])
        not in member.get(reverse("drive:file", args=[locked.pk])).content.decode()
    )
    assert _edit(member, locked, "y").status_code == 403  # type: ignore[attr-defined]
    assert _client(people["outsider"]).get(reverse("drive:edit_text", args=[locked.pk])).status_code == 404
