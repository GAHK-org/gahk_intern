"""Receivers for the koekken app (connected in `KoekkenConfig.ready`).

One `post_delete` receiver on `VagtBytte`: when an offer row is deleted, its Den Hurtige post (Amendment 4
step 3) is archived. Offers are almost never deleted directly -- they go by CASCADE when their
`VagtTildeling` is deleted (force re-run, override_remove, reconcile_month, declare_fridag, the vacated
row of a whole-shift take-over) or their offerer is. A cascade never calls `Model.delete()`, so a
`delete()` override would miss every one of them; a signal sees them all. (Same reasoning as Den
Hurtige's own image-cleanup receiver, den_hurtige.models._delete_image_file.)

It runs inside the deleter's transaction, after the offer row is actually deleted, so the lock order
holds: the post is locked last (LOCK ORDER level 5 in koekken.services). A receiver on VagtBytte disables
Django's fast-delete for it; that is fine at this scale.
"""

from typing import Any

from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import VagtBytte
from .services import _archive_posts


@receiver(post_delete, sender=VagtBytte)
def _archive_hurtig_post_on_delete(sender: type[VagtBytte], instance: VagtBytte, **kwargs: Any) -> None:  # noqa: ANN401
    if instance.hurtig_post_id is not None:
        _archive_posts([instance.hurtig_post_id])
