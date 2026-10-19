"""In-process domain events: how one domain reacts to another without either importing the other's
internals.

The publisher defines the event, as a frozen dataclass of primitives and ids, in its own `api.py` —
the event is part of its public contract. A subscriber registers a handler in its own `handlers.py`,
imported from its AppConfig.ready(). The publisher never learns who listens, which is the point:
`residents` can say "a resident opened the member area" without knowing that AK books its monthly
charge off the back of it. See docs/domain-boundaries.md.

HANDLERS RUN AFTER COMMIT, never inside the publisher's transaction. A handler that ran inline would
make the publisher's transaction fail for a reason in somebody else's domain, and would act on a
change a later rollback undoes. `transaction.on_commit` gives both guarantees; outside a transaction
(autocommit) it runs the handler immediately. `robust=True` logs a failing handler instead of raising
it into the publisher's request — by the time it runs, the publisher's work is committed and an
exception there could only turn a success into a 500.

Consequence for tests: pytest-django wraps each test in a transaction that never commits, so a test
that wants handlers to run uses `django_capture_on_commit_callbacks(execute=True)`.

Deliberately NOT Django signals: a signal is keyed on a Signal object and delivers `**kwargs`, so a
typo in a keyword is silent and nothing types the payload. Here the event type is the key and the
handler receives the dataclass itself.

Deliberately NOT durable either. If a handler must survive a crash (send a mail, call an outside
service), the handler's job is to enqueue a Celery task — that is the outbox, and Celery's
PostgreSQL broker already gives it. Keeping this module synchronous keeps it 60 lines long.
"""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from django.db import transaction


@dataclass(frozen=True)
class DomainEvent:
    """Base class for every published event. Subclasses are frozen dataclasses of ids and primitives
    — never a model instance, which the receiving domain could save and so reach into the publisher's
    data with."""


Handler = Callable[[DomainEvent], None]

_handlers: defaultdict[type[DomainEvent], list[Handler]] = defaultdict(list)


def subscribe[E: DomainEvent](event_type: type[E]) -> Callable[[Callable[[E], None]], Callable[[E], None]]:
    """Register the decorated function as a handler for `event_type`.

    Use it at module level in `<domain>/handlers.py`, and import that module from the domain's
    AppConfig.ready() — that import is what makes the registration happen exactly once per process.
    """

    def register(handler: Callable[[E], None]) -> Callable[[E], None]:
        if handler not in _handlers[event_type]:
            _handlers[event_type].append(handler)  # type: ignore[arg-type]
        return handler

    return register


def publish(event: DomainEvent) -> None:
    """Hand `event` to every handler of its exact type once the current transaction commits."""
    for handler in _handlers.get(type(event), ()):
        transaction.on_commit(_bound(handler, event), robust=True)


def _bound(handler: Handler, event: DomainEvent) -> Callable[[], None]:
    """`handler(event)` as a named function. Not functools.partial: a robust on_commit logs a failure
    as `callback.__qualname__`, which a partial does not have — so the failure would raise
    AttributeError out of the logging call instead of being logged."""

    def deliver() -> None:
        handler(event)

    deliver.__qualname__ = f"{handler.__module__}.{handler.__qualname__}({type(event).__name__})"
    return deliver


def handlers_for(event_type: type[DomainEvent]) -> list[Handler]:
    """The registered handlers, for tests that assert a subscription exists."""
    return list(_handlers.get(event_type, ()))
