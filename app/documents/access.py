from django.db.models import Q, QuerySet
from django.http import HttpRequest

from residents.models import Resident
from residents.permissions import current_resident

from .models import Document, DocumentGrant


def roles_allowed(_roles: object) -> bool:
    return True


def visible_documents(resident: Resident) -> QuerySet[Document]:
    return (
        Document.objects.filter(deleted_at__isnull=True)
        .filter(Q(owner=resident) | Q(grants__resident=resident))
        .distinct()
    )


def permission_for(document: Document, resident: Resident) -> str | None:
    if document.deleted_at is not None:
        return None
    if document.owner_id == resident.pk:
        return "owner"
    return (
        DocumentGrant.objects.filter(document=document, resident=resident)
        .values_list("permission", flat=True)
        .first()
    )


def can_view(document: Document, request: HttpRequest) -> bool:
    return permission_for(document, current_resident(request)) is not None


def can_edit(document: Document, request: HttpRequest) -> bool:
    return permission_for(document, current_resident(request)) in {"owner", DocumentGrant.Permission.EDITOR}


def can_manage(document: Document, request: HttpRequest) -> bool:
    return document.owner_id == current_resident(request).pk
