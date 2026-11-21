"""`Ny fil` in a folder, and the text editor (drive/text.py)."""

from uuid import UUID

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views.decorators.http import require_http_methods, require_POST

from residents.permissions import current_resident

from . import access, office, preview, text
from .access import Level
from .views import _folder, _render, crumbs, live_file


@access.access_required
@require_POST
def new_file(request: HttpRequest, uuid: UUID) -> HttpResponse:
    """A new text, markdown or CSV file, opened in the text editor; or a blank office file, opened for
    live editing while that is on."""
    folder, viewer, _ = _folder(request, uuid, at_least=Level.WRITE)
    kind, name = request.POST.get("kind", ""), request.POST.get("name", "")
    resident = current_resident(request)
    try:
        if kind in office.NEW:
            if not office.enabled():
                raise ValidationError("Redigering i browseren er ikke tilgængelig lige nu.")
            return redirect("drive:office", office.new_document(viewer, folder, kind, resident, name).uuid)
        return redirect("drive:edit_text", text.new_file(viewer, folder, kind, name, resident).uuid)
    except PermissionDenied:
        messages.error(request, "Du kan ikke lægge noget i den mappe.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect("drive:folder", folder.uuid)


@access.access_required
@require_http_methods(["GET", "POST"])
def edit(request: HttpRequest, uuid: UUID) -> HttpResponse:
    """The whole file in a text box. Saving makes the next version, unless somebody else saved one
    after the box was filled: then the text stays in the box, with a word on what happened."""
    file, viewer = live_file(request, uuid)
    if not text.editable(file):
        raise Http404("not a text file the editor opens")
    if not viewer.can_version(file):
        raise PermissionDenied
    latest = file.latest_version()
    conflict = False
    if request.method == "POST":
        content = request.POST.get("content", "")
        try:
            base = int(request.POST.get("base", ""))
        except ValueError:
            base = -1
        try:
            saved = text.save(viewer, file, content, base=base, resident=current_resident(request))
        except text.Conflict:
            conflict = True
            messages.error(
                request,
                "Filen er gemt af en anden, mens du skrev. Din tekst står herunder; den nyeste version "
                "kan ses i et andet vindue, før du gemmer igen.",
            )
            latest = file.latest_version()
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            if saved is None:
                messages.info(request, "Ingen ændringer at gemme.")
            else:
                messages.success(request, f"Gemt som version {saved.number}.")
            return redirect("drive:file", file.uuid)
    else:
        try:
            content = text.read(latest)
        except OSError:
            messages.error(request, "Filen kan ikke læses lige nu. Prøv igen om lidt.")
            return redirect("drive:file", file.uuid)
    return _render(
        request,
        "drive/text_edit.html",
        {
            "file": file,
            "ancestors": crumbs(viewer, file.folder),
            "content": content,
            "base": latest.number,
            "conflict": conflict,
            "markdown": preview.kind_of(file.name) == preview.Kind.MARKDOWN,
        },
    )
