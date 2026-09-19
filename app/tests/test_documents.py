# ruff: noqa: ANN001, ANN201

import json
from io import BytesIO
from unittest.mock import patch

import pytest
from django.urls import reverse

from documents import services
from documents.models import DocumentCallbackReceipt, DocumentGrant, DocumentSaveType, DocumentVersion


@pytest.mark.django_db
def test_create_document_persists_a_valid_initial_version(make_resident):
    owner = make_resident()

    document = services.create_document(owner=owner, title="Referat", document_type="docx")

    assert document.current_version is not None
    assert document.current_version.save_type == DocumentSaveType.INITIAL
    assert document.current_version.size > 0
    assert document.object_key.startswith(f"documents/{document.pk}/versions/")


@pytest.mark.django_db
def test_two_users_get_the_same_active_session_key(make_resident, rf, settings):
    settings.ONLYOFFICE_JWT_SECRET = "test-secret"
    owner = make_resident("owner@gahk.dk")
    collaborator = make_resident("editor@gahk.dk")
    document = services.create_document(owner=owner, title="Budget", document_type="xlsx")
    DocumentGrant.objects.create(
        document=document, resident=collaborator, permission="editor", granted_by=owner
    )
    request = rf.get("/")
    request.build_absolute_uri = lambda path="/": f"http://testserver{path}"

    with patch("documents.services.get_store") as store:
        store.return_value.signed_download_url.return_value = "http://minio/document.xlsx"
        owner_config = services.editor_config(
            document=document, resident=owner, can_edit=True, request=request
        )
        collaborator_config = services.editor_config(
            document=document, resident=collaborator, can_edit=True, request=request
        )

    assert owner_config["document"]["key"] == collaborator_config["document"]["key"]
    assert owner_config["token"] != collaborator_config["token"]


@pytest.mark.django_db
def test_viewer_editor_config_is_not_editable(client, make_resident):
    owner = make_resident("owner@gahk.dk")
    viewer = make_resident("viewer@gahk.dk")
    document = services.create_document(owner=owner, title="Plan", document_type="pptx")
    DocumentGrant.objects.create(document=document, resident=viewer, permission="viewer", granted_by=owner)
    client.force_login(viewer)

    with patch("documents.services.get_store") as store:
        store.return_value.signed_download_url.return_value = "http://minio/document.pptx"
        response = client.get(reverse("documents:editor_config", args=[document.pk]))

    assert response.status_code == 200
    assert response.json()["document"]["permissions"]["edit"] is False


@pytest.mark.django_db
def test_force_save_callback_is_idempotent(make_resident):
    owner = make_resident()
    document = services.create_document(owner=owner, title="Budget", document_type="xlsx")
    session = services.active_session(document.pk)
    payload = {"key": session.document_key, "status": 6, "url": "http://onlyoffice/cache/budget.xlsx"}
    raw = json.dumps(payload).encode()

    with patch("documents.services.download_callback_file", return_value=BytesIO(b"changed bytes")):
        assert services.persist_callback(document=document, payload=payload, raw_payload=raw) is True
        assert services.persist_callback(document=document, payload=payload, raw_payload=raw) is False

    assert DocumentVersion.objects.filter(document=document).count() == 2
    assert DocumentCallbackReceipt.objects.filter(session=session).count() == 1


@pytest.mark.django_db
def test_callback_accepts_onlyoffice_transport_token(client, make_resident, settings):
    settings.ONLYOFFICE_JWT_SECRET = "test-secret"
    owner = make_resident()
    document = services.create_document(owner=owner, title="Budget", document_type="xlsx")
    session = services.active_session(document.pk)
    payload = {"key": session.document_key, "status": 1}
    token = services.sign_jwt({**payload, "iat": 0, "exp": 2_000_000_000})

    response = client.post(
        reverse("documents:callback", args=[document.pk]),
        data=json.dumps({**payload, "token": token}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer unrelated-token",
    )

    assert response.status_code == 200
    assert response.json() == {"error": 0}
