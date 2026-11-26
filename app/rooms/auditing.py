"""What the audit trail keeps of Søg værelse (the room round) and Værelsestjek (room inspections).

Month fields here are absolute month numbers (rooms.kvotient.month_index); the names say the month in
words.
"""

from audit import trail

from .kvotient import month_label
from .models import (
    KvotientApplication,
    KvotientOrlov,
    KvotientPriority,
    RoomCondition,
    RoomConditionScore,
    RoomCriterion,
    RoomOffer,
)

trail.register(
    KvotientApplication,
    "rooms.application",
    name=lambda app: f"{app.resident.full_name}, {month_label(app.move_month)}",
    label="Værelsesansøgning",
    labels={
        "resident": "Beboer",
        "move_month": "Runde (måned)",
        "move_in_month": "Indflytning (måned)",
        "done_studying_month": "Færdig med studiet (måned)",
        "k": "Kvotient",
        "apply_datetime": "Søgt",
    },
)
trail.register(
    KvotientPriority,
    "rooms.priority",
    name=lambda p: f"{p.priority}. prioritet: {p.room}",
    label="Prioritet",
    labels={"application": "Ansøgning", "room": "Værelse", "priority": "Prioritet", "month": "Måned"},
)
trail.register(
    KvotientOrlov,
    "rooms.leave",
    name=lambda o: f"Orlov {o.number_of_months} md.",
    label="Orlov",
    labels={"application": "Ansøgning", "start_month": "Fra (måned)", "end_month": "Til (måned)"},
)
trail.register(
    RoomOffer,
    "rooms.offer",
    name=lambda offer: f"{offer.room}, {month_label(offer.month)}",
    label="Værelsestilbud",
    labels={"room": "Værelse", "month": "Måned", "awarded_application": "Vinder (lodtrækning)"},
)
trail.register(
    RoomCriterion,
    "rooms.criterion",
    name=lambda criterion: criterion.name,
    label="Tjekpunkt",
    labels={"code": "Kode", "name": "Navn", "description": "Forklaring", "options": "Skala"},
)
trail.register(
    RoomCondition,
    "rooms.condition",
    name=lambda condition: f"{condition.room}, {condition.recorded_at:%Y-%m-%d}",
    label="Værelsestjek",
    # Whether it is the room's newest report follows from the next report being made (the event that
    # makes it involves the one it replaces); the reporter's name is a copy of the event's actor.
    ignore={"is_current", "recorded_by_name"},
    labels={"room": "Værelse", "resident": "Tjekket af", "recorded_at": "Tjekket"},
)
trail.register(
    RoomConditionScore,
    "rooms.condition_score",
    name=lambda score: f"{score.criterion.name}: {'–' if score.score is None else score.score}",
    label="Svar i værelsestjek",
    labels={
        "condition": "Værelsestjek",
        "criterion": "Tjekpunkt",
        "score": "Karakter",
        "comment": "Kommentar",
        "image": "Billede (gammelt)",
        "photo": "Billede",
    },
)

trail.actions(
    {
        "rooms.application.submitted": "Søgte værelse",
        "rooms.application.updated": "Rettede sin værelsesansøgning",
        "rooms.application.withdrawn": "Trak sin værelsesansøgning tilbage",
        "rooms.application.deleted": "Slettede en værelsesansøgning",
        "rooms.offer.created": "Slog et værelse op",
        "rooms.offer.closed": "Lukkede et værelsestilbud",
        "rooms.offer.tie_resolved": "Afgjorde lodtrækningen om et værelse",
        "rooms.round.ended": "Afsluttede værelsesrunden",
        "rooms.condition.recorded": "Tjekkede et værelse",
    }
)
