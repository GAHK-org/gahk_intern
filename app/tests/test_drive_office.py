"""Drive: live editing in the Document Server (Euro-Office) - off unless configured and answering, the signed editor
configuration, the Document Server's own fetches and callbacks, new documents and editable copies.
The Document Server is played by a fake `urlopen`."""

import io
import json
from collections.abc import Callable, Iterator
from typing import Any
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.models import Workgroup
from drive import office, sharing
from drive.access import Viewer
from drive.models import File, Folder
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

SERVER = "https://office.example.invalid"
INTERNAL = "http://onlyoffice"
SECRET = "test-secret"


class FakeServer:
    """The Document Server, as far as this app talks to it: its health check, its conversions, and
    the files it serves."""

    def __init__(self) -> None:
        self.healthy = True
        self.files: dict[str, bytes] = {}
        self.converted: list[dict[str, Any]] = []
        self.fetched: list[str] = []

    def urlopen(self, request: Any, timeout: float = 0) -> io.BytesIO:  # noqa: ANN401
        url = request if isinstance(request, str) else request.full_url
        self.fetched.append(url)
        if url == f"{INTERNAL}/healthcheck":
            if not self.healthy:
                raise OSError("down")
            return io.BytesIO(b"true")
        if url == f"{INTERNAL}/ConvertService.ashx":
            body = json.loads(request.data)
            assert office.verify(body["token"])["outputtype"] == body["outputtype"]
            self.converted.append(body)
            self.files[f"{INTERNAL}/cache/converted"] = b"converted"
            return io.BytesIO(
                json.dumps({"endConvert": True, "fileUrl": f"{SERVER}/cache/converted"}).encode()
            )
        if url in self.files:
            return io.BytesIO(self.files[url])
        raise OSError(f"nothing at {url}")


@pytest.fixture
def server(settings: Any) -> Iterator[FakeServer]:  # noqa: ANN401
    settings.ONLYOFFICE_URL = SERVER
    settings.ONLYOFFICE_INTERNAL_URL = INTERNAL
    settings.ONLYOFFICE_APP_URL = "http://web:8000"
    settings.ONLYOFFICE_JWT_SECRET = SECRET
    cache.delete(office.HEALTH_CACHE_KEY)
    fake = FakeServer()
    with mock.patch.object(office.urllib.request, "urlopen", fake.urlopen):
        yield fake
    cache.delete(office.HEALTH_CACHE_KEY)


@pytest.fixture
def people(resident_in: Callable[..., Resident], group: Workgroup, open_gate: None) -> dict[str, Resident]:
    found = {
        "member": resident_in({(2026, 11): group}),
        "colleague": resident_in({(2026, 11): group}),
        "friend": resident_in({(2026, 11): None}),
    }
    ensure_structure()
    return found


def _client(resident: Resident) -> Client:
    client = Client()
    client.force_login(resident)
    return client


def _docx(client: Client, folder: Folder, name: str = "Referat.docx") -> File:
    assert _upload(client, folder, name, b"original")["status"] == 201
    return File.objects.get(folder=folder, name=name)


def _config(client: Client, file: File) -> dict[str, Any]:
    page = client.get(reverse("drive:office", args=[file.pk]))
    assert page.status_code == 200
    raw = page.content.decode().split('<script id="drive-office-config" type="application/json">')[1]
    return json.loads(raw.split("</script>")[0])


def _post_callback(file: File, data: dict[str, Any], *, secret: str = SECRET) -> dict[str, Any]:
    token = office.callback_url(file).rsplit("/", 1)[-1]
    with mock.patch("django.conf.settings.ONLYOFFICE_JWT_SECRET", secret):
        signed = office.sign(data)
    response = Client().post(
        reverse("drive:office_callback", args=[token]),
        json.dumps({**data, "token": signed}),
        content_type="application/json",
    )
    return {"status": response.status_code, **response.json()}


# --- Off -------------------------------------------------------------------------------------------


def test_off_by_default_office_files_are_downloaded(people: dict[str, Resident], group: Workgroup) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    file = _docx(member, winter)
    assert (
        reverse("drive:office", args=[file.pk])
        not in member.get(reverse("drive:file", args=[file.pk])).content.decode()
    )
    # It is there, greyed out, with a word on why.
    page = member.get(reverse("drive:file", args=[file.pk])).content.decode()
    assert "Rediger i browseren</button>" in page and "er slået fra" in page
    folder = member.get(reverse("drive:folder", args=[winter.pk])).content.decode()
    assert 'value="document" class="drive-new-file-kind" disabled' in folder and "er slået fra" in folder
    assert member.get(reverse("drive:office", args=[file.pk])).status_code == 404


def test_a_server_that_does_not_answer_switches_it_off(
    people: dict[str, Resident], group: Workgroup, server: FakeServer
) -> None:
    server.healthy = False
    member = _client(people["member"])
    file = _docx(member, _period_folder(group, WINTER))
    page = member.get(reverse("drive:file", args=[file.pk])).content.decode()
    assert reverse("drive:office", args=[file.pk]) not in page
    assert "svarer ikke lige nu" in page
    assert member.get(reverse("drive:office", args=[file.pk])).status_code == 404


# --- Opening ---------------------------------------------------------------------------------------


def test_a_member_edits_and_the_signed_configuration_names_only_the_resident(
    people: dict[str, Resident], group: Workgroup, server: FakeServer
) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    file = _docx(member, winter)
    assert ">Rediger</a>" in member.get(reverse("drive:file", args=[file.pk])).content.decode()
    folder = member.get(reverse("drive:folder", args=[winter.pk])).content.decode()
    assert 'value="spreadsheet" class="drive-new-file-kind">' in folder  # in `Ny fil`, not greyed out

    config = _config(member, file)
    signed = office.verify(config.pop("token"))
    assert signed == config
    assert config["editorConfig"]["mode"] == "edit"
    assert config["editorConfig"]["user"] == {
        "id": str(people["member"].pk),
        "name": people["member"].full_name,
    }
    assert config["editorConfig"]["callbackUrl"].startswith("http://web:8000/drive/office/callback/")

    # The free editions' phone editor only reads, so a phone that may edit gets the computer layout,
    # and is told so.
    phone = Client(headers={"User-Agent": "Mozilla/5.0 (iPhone) Mobile/15E148"})
    phone.force_login(people["member"])
    on_phone = _config(phone, file)
    assert (on_phone["type"], on_phone["editorConfig"]["mode"]) == ("desktop", "edit")
    page = phone.get(reverse("drive:office", args=[file.pk])).content.decode()
    assert "computerudgaven af editoren" in page

    # Everyone on the same version shares the session's key.
    assert _config(_client(people["colleague"]), file)["document"]["key"] == config["document"]["key"]

    # The Document Server fetches the document without anybody's login.
    fetch = config["document"]["url"].removeprefix("http://web:8000")
    response = Client().get(fetch)
    assert b"".join(response.streaming_content) == b"original"
    assert Client().get(fetch[:-3] + "xyz").status_code == 404


def test_a_reader_and_a_locked_file_open_to_view(
    people: dict[str, Resident], group: Workgroup, server: FakeServer
) -> None:
    member = _client(people["member"])
    file = _docx(member, _period_folder(group, WINTER))
    sharing.share(
        Viewer.for_resident(people["member"]),
        file=file,
        resident_id=people["friend"].pk,
        access="read",
        actor=people["member"],
    )
    friend = _client(people["friend"])
    assert ">Åbn</a>" in friend.get(reverse("drive:file", args=[file.pk])).content.decode()
    assert _config(friend, file)["editorConfig"]["mode"] == "view"
    # Reading on a phone: the phone editor.
    friend_phone = Client(headers={"User-Agent": "Mozilla/5.0 (iPhone) Mobile/15E148"})
    friend_phone.force_login(people["friend"])
    assert (_config(friend_phone, file)["type"], _config(friend_phone, file)["editorConfig"]["mode"]) == (
        "mobile",
        "view",
    )

    file.locked_at = timezone.now()
    file.save()
    assert _config(member, file)["editorConfig"]["mode"] == "view"


# --- Saving ----------------------------------------------------------------------------------------


def test_the_edited_document_becomes_a_version_listing_who_edited(
    people: dict[str, Resident], group: Workgroup, server: FakeServer
) -> None:
    member = _client(people["member"])
    file = _docx(member, _period_folder(group, WINTER))
    key = _config(member, file)["document"]["key"]
    users = [str(people["member"].pk), str(people["colleague"].pk)]

    assert _post_callback(file, {"key": key, "status": 1, "users": users})["error"] == 0
    file.refresh_from_db()
    assert file.in_live_edit()
    replace = _upload(member, file.folder, "Referat.docx", b"upload", on_conflict="replace")
    assert replace["status"] == 400
    assert "redigeres lige nu" in replace["error"]

    server.files[f"{INTERNAL}/cache/edited.docx"] = b"edited"
    # For a save, `users` is only the last to edit; the history has everyone who changed something.
    changes = [{"user": {"id": user, "name": "x"}} for user in users]
    saved = _post_callback(
        file,
        {
            "key": key,
            "status": 2,
            "users": users[1:],
            "history": {"changes": changes},
            "url": f"{SERVER}/cache/edited.docx",
        },
    )
    assert saved["error"] == 0
    file.refresh_from_db()
    assert not file.in_live_edit()
    version = file.latest_version()
    assert version.number == 2
    assert set(version.editors.all()) == {people["member"], people["colleague"]}
    page = member.get(reverse("drive:file", args=[file.pk])).content.decode()
    assert f"redigeret af {people['member'].full_name}, {people['colleague'].full_name}" in page
    assert _config(member, file)["document"]["key"] != key  # a new version, a new session


def test_a_callback_must_be_signed_and_name_the_document_server(
    people: dict[str, Resident], group: Workgroup, server: FakeServer
) -> None:
    member = _client(people["member"])
    file = _docx(member, _period_folder(group, WINTER))
    key = _config(member, file)["document"]["key"]
    assert _post_callback(file, {"key": key, "status": 1}, secret="wrong")["status"] == 403

    server.files["http://169.254.169.254/latest"] = b"secret"
    answer = _post_callback(file, {"key": key, "status": 2, "url": "http://169.254.169.254/latest"})
    assert answer["error"] == 1
    assert "http://169.254.169.254/latest" not in server.fetched
    assert file.versions.count() == 1


def test_work_on_a_file_locked_meanwhile_is_kept_beside_it(
    people: dict[str, Resident], group: Workgroup, server: FakeServer
) -> None:
    member = _client(people["member"])
    file = _docx(member, _period_folder(group, WINTER))
    key = _config(member, file)["document"]["key"]
    file.locked_at = timezone.now()
    file.save()
    server.files[f"{INTERNAL}/cache/edited.docx"] = b"edited"
    users = [str(people["member"].pk)]
    assert (
        _post_callback(file, {"key": key, "status": 2, "users": users, "url": f"{SERVER}/cache/edited.docx"})[
            "error"
        ]
        == 0
    )
    assert file.versions.count() == 1
    kept = File.objects.get(name="Referat (2).docx")
    assert kept.latest_version().editors.get() == people["member"]


# --- New documents and copies ------------------------------------------------------------------------


def test_new_documents_are_made_blank_and_opened(
    people: dict[str, Resident], group: Workgroup, server: FakeServer
) -> None:
    member = _client(people["member"])
    winter = _period_folder(group, WINTER)
    first = member.post(reverse("drive:new_file", args=[winter.pk]), {"kind": "presentation"})
    made = File.objects.get(name="Ny præsentation.pptx")
    assert first["Location"] == reverse("drive:office", args=[made.pk])
    assert made.size > 0
    member.post(reverse("drive:new_file", args=[winter.pk]), {"kind": "presentation"})
    assert File.objects.filter(name="Ny præsentation (2).pptx").exists()
    member.post(reverse("drive:new_file", args=[winter.pk]), {"kind": "spreadsheet", "name": "Budget 2027"})
    assert File.objects.filter(name="Budget 2027.xlsx").exists()
    friend = _client(people["friend"])
    assert friend.post(reverse("drive:new_file", args=[winter.pk]), {"kind": "document"}).status_code == 404


def test_an_older_format_opens_to_view_and_rediger_makes_a_copy(
    people: dict[str, Resident], group: Workgroup, server: FakeServer
) -> None:
    member = _client(people["member"])
    file = _docx(member, _period_folder(group, WINTER), "Vedtægter.doc")
    page = member.get(reverse("drive:file", args=[file.pk])).content.decode()
    assert ">Åbn</a>" in page
    assert "Rediger en kopi" in page
    assert _config(member, file)["editorConfig"]["mode"] == "view"

    response = member.post(reverse("drive:office_convert", args=[file.pk]))
    copy = File.objects.get(name="Vedtægter.docx")
    assert response["Location"] == reverse("drive:office", args=[copy.pk])
    assert server.converted[0]["filetype"] == "doc"
    assert server.converted[0]["outputtype"] == "docx"
    assert copy.created_by == people["member"]
