"""What the audit trail keeps of access: nothing of its own yet. Roles live in groups the sync keeps
from facts the trail already records."""

from audit import trail

from .models import AccessRule

trail.exempt(AccessRule, "no table: it only carries the access permissions")
