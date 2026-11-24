"""What the audit trail keeps of grants."""

from audit import trail

from .models import Grant
from .policy import ROLE_LABELS

trail.register(
    Grant,
    "access.grant",
    name=lambda g: f"{g.resident.full_name}: {ROLE_LABELS.get(g.role, g.role)}",
    label="Adgang givet",
    labels={
        "resident": "Konto",
        "role": "Rolle",
        "reason": "Hvorfor",
        "starts_on": "Fra",
        "ends_on": "Til og med",
        "granted_by": "Givet af",
        "created_at": "Oprettet",
    },
)

trail.actions(
    {
        "access.grant.added": "Gav adgang",
        "access.grant.ended": "Afsluttede adgang",
        "access.kitchen_staff.added": "Oprettede køkkenpersonale",
    }
)
