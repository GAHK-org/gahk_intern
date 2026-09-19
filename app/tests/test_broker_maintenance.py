"""Sweeping the broker's own table (core.tasks.purge_delivered_broker_messages).

kombu's SQLAlchemy transport never deletes a message it has delivered — taking one only flips
`visible` to false — and nothing else pruned the table, so it grew by one payload-carrying row per
message ever sent. `process_pending_media` contributes one recovery message each night.

The table belongs to kombu, not to Django, so it does not exist in a fresh test database. These
build it to the shape the running database actually has (verified against it: `timestamp` is
`timestamp without time zone`, holding UTC) rather than mocking the query away, because the two
things worth pinning — which rows are chosen, and that the cutoff is compared as naive UTC — are
exactly what a mock would assume rather than test.
"""

import datetime

import pytest
from django.db import connection

from core.tasks import BROKER_MESSAGE_RETENTION, purge_delivered_broker_messages

KOMBU_TABLE = """
    CREATE TABLE kombu_message (
        id serial PRIMARY KEY,
        visible boolean,
        timestamp timestamp without time zone,
        payload text,
        version smallint,
        queue_id integer
    )
"""


def _insert(visible: bool, age: datetime.timedelta) -> None:
    stamp = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - age
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO kombu_message (visible, timestamp, payload, version, queue_id)"
            " VALUES (%s, %s, '{}', 1, 1)",
            [visible, stamp],
        )


@pytest.mark.django_db
def test_the_sweep_is_a_noop_when_the_transport_is_not_the_database() -> None:
    """Pointing CELERY_BROKER_URL at Redis or RabbitMQ leaves no such table, and that has to be a
    quiet zero rather than a nightly ProgrammingError in the worker log."""
    assert purge_delivered_broker_messages.run() == 0


@pytest.mark.django_db
def test_only_delivered_messages_past_the_retention_window_are_swept() -> None:
    old = BROKER_MESSAGE_RETENTION + datetime.timedelta(hours=1)
    recent = datetime.timedelta(minutes=5)
    with connection.cursor() as cursor:
        cursor.execute(KOMBU_TABLE)
    _insert(visible=False, age=old)  # delivered and past the window — the only one that may go
    _insert(visible=False, age=recent)  # delivered, but still inside the window
    _insert(visible=True, age=old)  # OLD BUT UNDELIVERED: deleting this loses a queued job

    assert purge_delivered_broker_messages.run() == 1

    with connection.cursor() as cursor:
        cursor.execute("SELECT visible FROM kombu_message ORDER BY id")
        assert cursor.fetchall() == [(False,), (True,)]
