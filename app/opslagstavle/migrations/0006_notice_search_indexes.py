"""Indexes for the board's search (opslagstavle/search.py), Postgres only.

Raw SQL because both are expression indexes that must match what search.py makes Django emit,
character for character, or the planner ignores them. Skipped on SQLite, where search falls back to
substring matching.

pg_trgm is a trusted extension (Postgres 13+), so the database owner can create it. If the server
refuses anyway, the trigram index is skipped inside a savepoint rather than failing the deploy:
search still works, just without that index.
"""

import logging

from django.db import migrations, transaction

FTS = (
    "CREATE INDEX IF NOT EXISTS notice_body_fts_idx ON opslagstavle_notice "
    "USING gin (to_tsvector('danish'::regconfig, COALESCE(body, '')))"
)
TRIGRAM = (
    "CREATE INDEX IF NOT EXISTS notice_body_trgm_idx ON opslagstavle_notice "
    "USING gin (UPPER(body::text) gin_trgm_ops)"
)


def create(apps, schema_editor):  # noqa: ANN001, ANN201
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(FTS)
    try:
        with transaction.atomic(using=schema_editor.connection.alias):
            schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            schema_editor.execute(TRIGRAM)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("pg_trgm unavailable; search runs without the trigram index")


def drop(apps, schema_editor):  # noqa: ANN001, ANN201
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("DROP INDEX IF EXISTS notice_body_trgm_idx")
    schema_editor.execute("DROP INDEX IF EXISTS notice_body_fts_idx")


class Migration(migrations.Migration):
    dependencies = [("opslagstavle", "0005_noticecomment_image_alter_noticecomment_body")]

    operations = [migrations.RunPython(create, drop)]
