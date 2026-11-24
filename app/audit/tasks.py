from celery import shared_task
from django.db import connection, transaction

from . import trail


@shared_task
def purge_expired() -> int:
    """Delete the events whose retention ran out, with their entities and changes, and record how
    many. The models refuse deletes; the database lets these through."""
    # In each statement, the first condition is the shortest period in audit_expired: it lets the
    # `at` index find the candidates before the rule is asked. now() is the transaction's start, so
    # all three see the same events.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM audit_auditchange WHERE entity_id IN (SELECT n.id FROM audit_auditentity n "
            "JOIN audit_auditevent e ON e.id = n.event_id "
            "WHERE e.at < now() - interval '1 year' AND audit_expired(e.at, e.action))"
        )
        cursor.execute(
            "DELETE FROM audit_auditentity WHERE event_id IN (SELECT id FROM audit_auditevent "
            "WHERE at < now() - interval '1 year' AND audit_expired(at, action))"
        )
        cursor.execute(
            "DELETE FROM audit_auditevent WHERE at < now() - interval '1 year' AND audit_expired(at, action)"
        )
        purged = cursor.rowcount
    if purged:
        trail.record("audit.expired.purged", actor=None, details={"count": purged})
    return purged
