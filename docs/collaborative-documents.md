# Collaborative documents

`documents` is the document manager. Django/PostgreSQL own titles, owners, grants, active editing sessions, callbacks, and version history. The private S3 bucket owns immutable OOXML files under `documents/<document-id>/versions/`. ONLYOFFICE Docs is the browser editor and active co-editing cache.

## Local setup

Run `docker compose up -d onlyoffice` and configure the same `ONLYOFFICE_JWT_SECRET` for Django and the container. For `task dev`, Django uses `http://web:8000` as `DOCUMENT_PUBLIC_URL` and signs MinIO links for `http://minio:9000`. For `task dev:local`, configure `DOCUMENT_PUBLIC_URL=http://host.docker.internal:8800` and `DOCUMENT_S3_ENDPOINT_URL=http://host.docker.internal:9000`. Then run `python app/manage.py migrate` and open `/intern/dokumenter/`.

ONLYOFFICE is published on port 8088 locally. Production must publish it through HTTPS at `ONLYOFFICE_PUBLIC_URL`, while `ONLYOFFICE_INTERNAL_URL` remains a private Docker or proxy address. Keep its `Data` and log volumes across restarts and ensure the reverse proxy supports WebSockets.

## Security and persistence

Django signs editor configurations and ONLYOFFICE callbacks with HS256 JWT. The secret is never sent to the browser. Editor/viewer status is calculated for every request from the owner/grant tables, so modifying browser JavaScript cannot grant editor rights. Downloads recheck authorization. The editor receives only a short-lived presigned URL for the current private object.

Callback downloads accept only absolute URLs at `ONLYOFFICE_INTERNAL_URL`, reject redirects, and stream with `DOCUMENT_MAX_FILE_SIZE` as a hard ceiling. A locked editing session serializes saves; the raw callback digest is stored as an idempotency receipt. S3 upload completes before a new current version is committed, so an interrupted upload can leave only an unreferenced immutable object, never a version pointing at missing bytes.

Celery Beat calls ONLYOFFICE's documented `forcesave` Command Service every `DOCUMENT_FORCE_SAVE_INTERVAL` seconds, defaulting to five minutes. Status-6 callbacks create snapshots without closing the shared session or changing its key. Identical bytes are skipped. Status 2 creates a final version and closes the session. Edits after the latest successful snapshot remain in ONLYOFFICE's active persistent state and may need its own recovery after a Document Server failure.

Restoration is owner-only and refuses while a collaborative session is active. It copies a selected immutable object into a new restore version rather than overwriting history.

## Operations and testing

Required production environment values are `ONLYOFFICE_PUBLIC_URL`, `ONLYOFFICE_INTERNAL_URL`, `ONLYOFFICE_JWT_SECRET`, `DOCUMENT_PUBLIC_URL`, `DOCUMENT_S3_ENDPOINT_URL`, and the existing private S3 settings. Set `DOCUMENT_PUBLIC_URL` to the URL that the Document Server can reach for callbacks, not to a browser-only hostname. Run migrations with `python app/manage.py migrate`.

Run `task test:sqlite -- tests/test_documents.py` for persistence tests. For integration testing, open one document from two independent browser sessions as users with editor grants: both configs must have the same `document.key`, and changes must appear in both editors. Leave it open past one force-save interval and confirm a `force_save` version exists. Close both editors and confirm a `final` version exists and its object key is in the private bucket.

For callback failures, check Django logs for the document UUID and ONLYOFFICE logs for the callback request. Check the private DNS route from ONLYOFFICE to `DOCUMENT_PUBLIC_URL`, the signed S3 route from ONLYOFFICE to `DOCUMENT_S3_ENDPOINT_URL`, matching JWT secrets, and clock synchronization. The Community Document Server license has concurrent-editing limits; verify the pinned version's license and capacity before production.