import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from residents.models import Resident
from residents.permissions import current_resident

from . import access, services
from .forms import DocumentCreateForm, DocumentGrantForm
from .models import Document, DocumentGrant, DocumentVersion
from .storage import get_store

logger = logging.getLogger(__name__)


def _document_for_view(request: HttpRequest, document_id: str) -> Document:
    document = get_object_or_404(Document, pk=document_id, deleted_at__isnull=True)
    if not access.can_view(document, request):
        raise Http404("Document findes ikke")
    return document


@login_required
def index(request: HttpRequest) -> HttpResponse:
    resident = current_resident(request)
    form = DocumentCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            document = services.create_document(owner=resident, **form.cleaned_data)
        except Exception:
            logger.exception("Document creation failed", extra={"resident_id": resident.pk})
            messages.error(request, "Dokumentet kunne ikke oprettes.")
        else:
            return redirect("documents:editor", document_id=document.pk)
    return render(
        request, "documents/index.html", {"documents": access.visible_documents(resident), "form": form}
    )


@login_required
def editor(request: HttpRequest, document_id: str) -> HttpResponse:
    document = _document_for_view(request, document_id)
    editable = access.can_edit(document, request)
    try:
        config = services.editor_config(
            document=document, resident=current_resident(request), can_edit=editable, request=request
        )
    except RuntimeError as exc:
        messages.error(request, str(exc))
        return redirect("documents:index")
    return render(
        request,
        "documents/editor.html",
        {
            "document": document,
            "editor_config": config,
            "onlyoffice_public_url": settings.ONLYOFFICE_PUBLIC_URL,
        },
    )


@login_required
def editor_fullscreen(request: HttpRequest, document_id: str) -> HttpResponse:
    document = _document_for_view(request, document_id)
    editable = access.can_edit(document, request)
    try:
        config = services.editor_config(
            document=document, resident=current_resident(request), can_edit=editable, request=request
        )
    except RuntimeError as exc:
        messages.error(request, str(exc))
        return redirect("documents:index")
    return render(
        request,
        "documents/editor_fullscreen.html",
        {
            "document": document,
            "editor_config": config,
            "onlyoffice_public_url": settings.ONLYOFFICE_PUBLIC_URL,
        },
    )


@login_required
def editor_config(request: HttpRequest, document_id: str) -> JsonResponse:
    document = _document_for_view(request, document_id)
    return JsonResponse(
        services.editor_config(
            document=document,
            resident=current_resident(request),
            can_edit=access.can_edit(document, request),
            request=request,
        )
    )


@login_required
def download(request: HttpRequest, document_id: str, version_id: str | None = None) -> StreamingHttpResponse:
    document = _document_for_view(request, document_id)
    version = document.current_version
    if version_id is not None:
        version = get_object_or_404(DocumentVersion, pk=version_id, document=document)
    if version is None:
        raise Http404("Documentet har ingen version")
    response = StreamingHttpResponse(
        get_store().chunks(version.object_key), content_type="application/octet-stream"
    )
    response["Content-Disposition"] = f'attachment; filename="{document.original_filename}"'
    return response


@login_required
def versions(request: HttpRequest, document_id: str) -> HttpResponse:
    document = _document_for_view(request, document_id)
    return render(
        request, "documents/versions.html", {"document": document, "versions": document.versions.all()}
    )


@login_required
@require_POST
def restore(request: HttpRequest, document_id: str, version_id: str) -> HttpResponse:
    document = _document_for_view(request, document_id)
    if not access.can_manage(document, request):
        raise PermissionDenied
    version = get_object_or_404(DocumentVersion, pk=version_id, document=document)
    try:
        services.restore_version(document=document, version=version)
    except ValueError:
        messages.error(request, "Dokumentet kan ikke gendannes, mens nogen redigerer det.")
    else:
        messages.success(request, "Versionen er gendannet som en ny version.")
    return redirect("documents:versions", document_id=document.pk)


@login_required
@require_POST
def grant_access(request: HttpRequest, document_id: str) -> HttpResponse:
    document = _document_for_view(request, document_id)
    if not access.can_manage(document, request):
        raise PermissionDenied
    form = DocumentGrantForm(request.POST)
    if form.is_valid():
        resident = Resident.objects.filter(email__iexact=form.cleaned_data["email"]).first()
        if resident is None or resident.pk == document.owner_id:
            messages.error(request, "Beboeren findes ikke.")
        else:
            DocumentGrant.objects.update_or_create(
                document=document,
                resident=resident,
                defaults={
                    "permission": form.cleaned_data["permission"],
                    "granted_by": current_resident(request),
                },
            )
            messages.success(request, "Adgang er opdateret.")
    else:
        messages.error(request, "Adgang kunne ikke opdateres.")
    return redirect("documents:versions", document_id=document.pk)


@login_required
@require_POST
def delete(request: HttpRequest, document_id: str) -> HttpResponse:
    document = _document_for_view(request, document_id)
    if not access.can_manage(document, request):
        raise PermissionDenied
    if document.editing_sessions.filter(state="active").exists():
        messages.error(request, "Luk redigeringen, før dokumentet slettes.")
    else:
        document.deleted_at = timezone.now()
        document.save(update_fields=["deleted_at", "updated_at"])
    return redirect("documents:index")


@csrf_exempt
@require_POST
def callback(request: HttpRequest, document_id: str) -> JsonResponse:
    authorization = request.headers.get("Authorization", "")
    header_token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = json.loads(request.body)
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        body_token = payload.get("token")
        claims = services.verify_jwt(body_token) if isinstance(body_token, str) else None
        if claims is None:
            claims = services.verify_jwt(header_token)
        signed_payload = {key: value for key, value in payload.items() if key != "token"}
        callback_claims = {key: value for key, value in (claims or {}).items() if key not in {"iat", "exp"}}
        # ONLYOFFICE appends the transport token after signing. All other callback fields must
        # exactly match the JWT claims so the token cannot authorize a substituted payload.
        if callback_claims != signed_payload:
            raise ValueError("callback JWT does not match payload")
        document = Document.objects.get(pk=document_id, deleted_at__isnull=True)
        services.persist_callback(document=document, payload=payload, raw_payload=request.body)
    except (Document.DoesNotExist, ValueError, json.JSONDecodeError, KeyError):
        logger.warning("Rejected ONLYOFFICE callback for document %s", document_id)
        return JsonResponse({"error": 1}, status=400)
    except Exception:
        logger.exception("ONLYOFFICE callback persistence failed for document %s", document_id)
        return JsonResponse({"error": 1}, status=500)
    return JsonResponse({"error": 0})
