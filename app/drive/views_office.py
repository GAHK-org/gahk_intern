"""Live editing: the editor page, a new document, an editable copy of an older format - and the two
addresses the Document Server itself calls, which have no login: fetching a document by a signed
link, and posting back the result (drive/office.py)."""

import json
from uuid import UUID

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from residents.permissions import current_resident

from . import access, office, services
from .access import Level
from .views import _folder, _render, crumbs, live_file


def _on() -> None:
    if not office.enabled():
        raise Http404("OnlyOffice is off")


@access.access_required
def editor(request: HttpRequest, uuid: UUID) -> HttpResponse:
    """The file in the editor: to edit for whoever could upload a new version of it (an edit share
    is enough), otherwise - and for the older formats, and a locked file - to view."""
    _on()
    file, viewer = live_file(request, uuid)
    if office.document_type(file.name) is None:
        raise Http404("not an office file")
    phone = "Mobi" in request.headers.get("User-Agent", "")
    edit = office.editable(file.name) and viewer.can_version(file)
    config = office.editor_config(
        file,
        file.latest_version(),
        current_resident(request),
        edit=edit,
        back_url=request.build_absolute_uri(reverse("drive:file", args=[file.uuid])),
        # The phone editor only reads in the free editions (office.editor_config): a phone that may
        # edit gets the computer layout instead.
        phone=phone and not edit,
    )
    return _render(
        request,
        "drive/office.html",
        {
            "file": file,
            "ancestors": crumbs(viewer, file.folder),
            "config": config,
            "api_url": f"{settings.ONLYOFFICE_URL}/web-apps/apps/api/documents/api.js",
            "edit": edit,
            "edit_on_phone": edit and phone,
        },
    )


@access.access_required
@require_POST
def new_document(request: HttpRequest, uuid: UUID) -> HttpResponse:
    _on()
    folder, viewer, _ = _folder(request, uuid, at_least=Level.WRITE)
    try:
        file = office.new_document(viewer, folder, request.POST.get("kind", ""), current_resident(request))
    except PermissionDenied:
        messages.error(request, "Du kan ikke lægge noget i den mappe.")
        return redirect("drive:folder", folder.uuid)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("drive:folder", folder.uuid)
    return redirect("drive:office", file.uuid)


@access.access_required
@require_POST
def convert(request: HttpRequest, uuid: UUID) -> HttpResponse:
    """`Rediger` on a doc, xls, ppt, odt …: a docx, xlsx or pptx copy beside it, opened to edit."""
    _on()
    file, viewer = live_file(request, uuid)
    try:
        copy = office.convert(viewer, file, current_resident(request))
    except PermissionDenied:
        messages.error(request, "Du kan ikke lægge en kopi i den mappe.")
        return redirect("drive:file", file.uuid)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("drive:file", file.uuid)
    messages.success(request, f"Kopien „{copy.name}“ ligger ved siden af originalen.")
    return redirect("drive:office", copy.uuid)


def fetch(request: HttpRequest, token: str) -> HttpResponseBase:
    """The Document Server fetching a version Drive opened in it. No login: the signed link is the
    key, and opens this one version for FETCH_HOURS."""
    version = office.fetched_version(token) if office.configured() else None
    if version is None:
        raise Http404("no such document")
    response = FileResponse(
        services.storage().open(version.key, "rb"), content_type="application/octet-stream"
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@csrf_exempt
@require_POST
def callback(request: HttpRequest, token: str) -> JsonResponse:
    """The Document Server's status posts about one file (office.callback). Its body is signed with
    the shared secret, as a `token` field or in the Authorization header; anything else is refused."""
    file_uuid = office.called_back_file(token) if office.configured() else None
    if file_uuid is None:
        raise Http404("no such document")
    try:
        body = json.loads(request.body or b"{}")
        if isinstance(body, dict) and body.get("token"):
            data = office.verify(str(body["token"]))
        else:
            header = request.headers.get("Authorization", "")
            data = office.verify(header.removeprefix("Bearer ")).get("payload", {})
    except (ValueError, office.BadToken):
        return JsonResponse({"error": 1, "message": "bad token"}, status=403)
    if not isinstance(data, dict):
        return JsonResponse({"error": 1}, status=400)
    return JsonResponse({"error": 0 if office.callback(file_uuid, data) else 1})
