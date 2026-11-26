"""What the audit trail keeps of access: the grants. Roles live in groups the sync keeps from facts
the trail already records (the alumneliste, grants, accounts)."""

from audit import trail

from .models import AccessRule, Grant
from .policy import ROLE_LABELS

trail.exempt(AccessRule, "no table: it only carries the access permissions")

trail.register(
    Grant,
    "access.grant",
    name=lambda g: f"{g.account.full_name}: {ROLE_LABELS.get(g.role, g.role)}",
    label="Rolle givet",
    labels={
        "account": "Konto",
        "role": "Rolle",
        "reason": "Hvorfor",
        "starts_on": "Fra",
        "ends_on": "Til og med",
        "granted_by": "Givet af",
        "created_at": "Oprettet",
    },
)
