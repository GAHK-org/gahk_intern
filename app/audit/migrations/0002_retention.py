from django.db import migrations

# How long the trail is kept (decided 2026-11-23): a year for logins, logouts and reset requests, five
# years for everything else, as long as bookkeeping is kept. The rule lives here only, in the
# database: the trigger lets a DELETE through only once the row's event has expired, and the nightly
# purge (audit.tasks.purge_expired) deletes by the same function. UPDATE stays refused for every row.
# Changing a period is a new migration that replaces audit_expired.
RETENTION = [
    """
    CREATE FUNCTION audit_expired(at timestamptz, action varchar) RETURNS boolean
    LANGUAGE sql STABLE AS $$
        SELECT at < now() - CASE
            WHEN action IN ('auth.login', 'auth.login.failed', 'auth.logout', 'auth.password_reset.requested')
            THEN interval '1 year'
            ELSE interval '5 years'
        END
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        event_at timestamptz;
        event_action varchar;
    BEGIN
        IF TG_OP = 'DELETE' THEN
            IF TG_TABLE_NAME = 'audit_auditevent' THEN
                event_at := OLD.at;
                event_action := OLD.action;
            ELSIF TG_TABLE_NAME = 'audit_auditentity' THEN
                SELECT e.at, e.action INTO event_at, event_action
                FROM audit_auditevent e WHERE e.id = OLD.event_id;
            ELSE
                SELECT e.at, e.action INTO event_at, event_action
                FROM audit_auditentity n JOIN audit_auditevent e ON e.id = n.event_id
                WHERE n.id = OLD.entity_id;
            END IF;
            IF event_at IS NOT NULL AND audit_expired(event_at, event_action) THEN
                RETURN OLD;
            END IF;
        END IF;
        RAISE EXCEPTION 'The audit trail is append-only: % on % refused', TG_OP, TG_TABLE_NAME;
    END;
    $$
    """,
]
RETENTION_REVERSE = [
    """
    CREATE OR REPLACE FUNCTION audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION 'The audit trail is append-only: % on % refused', TG_OP, TG_TABLE_NAME;
    END;
    $$
    """,
    "DROP FUNCTION audit_expired(timestamptz, varchar)",
]


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial")]

    operations = [migrations.RunSQL(RETENTION, RETENTION_REVERSE)]
