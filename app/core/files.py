"""Deleting uploaded files along with the rows that reference them.

Django stopped deleting FileField files on row delete in 1.3, so without this the database row goes
and the upload stays forever — invisible, because nothing lists orphaned files and nothing else ever
cleans them up. For a feature built on the promise that content is deleted (Den Hurtige's 30 minutes,
opslagstavlen's two years) that is the *opposite* of what the feature promises: the text expires on
schedule while the photograph does not.

Callers register this as a `post_delete` receiver rather than overriding `Model.delete()`, and both
halves of that matter:

  * a bulk queryset delete (a retention purge) never calls `Model.delete()`, and
  * cascaded children are removed by the database, never deleted directly,

so an override would silently miss exactly the paths that delete the most. The trade-off is that a
post_delete receiver disables Django's fast-delete optimisation, so a purge fetches rows before
removing them — irrelevant at this kollegium's volume, and the price of the files actually going.

Fields are discovered from the model rather than named, so one implementation covers
QuickPost.image, QuickComment.image, CmsImage.file, RoomConditionScore.photo and NoticeImage.file.

TWO THINGS HERE EXIST BECAUSE THE FILES MOVED TO OBJECT STORAGE, and both would be pointless
complexity against a local disk:

**The delete waits for the commit.** `post_delete` fires *inside* the transaction Django wraps
around `delete()`, so if anything later in an enclosing atomic block fails, the row comes back and
the file is already gone. Against a local filesystem that window is microseconds; against a bucket it
is a network round trip, and there are real atomic blocks around these paths (opslagstavle.views'
save-then-sync_images, residents.views, rooms.views). `transaction.on_commit` moves the delete to
after the commit that made the row's disappearance real. Outside an atomic block it runs
immediately, so nothing changes for the simple cases.

**A failed delete is logged, not raised.** Unlinking a local file essentially cannot fail; a DELETE
over the network routinely can, and there is no useful recovery — the row is already gone and the
transaction already committed. Raising would turn a Hetzner blip into a 500 on the Den Hurtige feed,
which purges expired posts on every load. So the object leaks instead, and `audit_media` is what
finds leaks; a broken feed is worse than a stray object nobody can see.
"""

import logging

from django.core.files.storage import Storage
from django.db import models, transaction

logger = logging.getLogger(__name__)


def delete_attached_files(instance: models.Model) -> None:
    """Delete every FileField file on `instance` from storage, after the current transaction commits.

    Safe to call twice: a delete of a name that is already gone is a no-op on both backends.
    """
    # Resolved now, not in the callback: `instance` is about to go out of scope, and reading the
    # field afterwards would be reading a model whose row no longer exists.
    targets: list[tuple[Storage, str]] = []
    for field in instance._meta.get_fields():
        # _meta is Django's documented model-introspection API despite the underscore.
        if not isinstance(field, models.FileField):
            continue
        file = getattr(instance, field.name, None)
        if file:
            targets.append((file.storage, file.name))

    if not targets:
        return

    label = f"{instance._meta.label}:{instance.pk}"

    def _purge() -> None:
        for storage, name in targets:
            try:
                storage.delete(name)
            except Exception:
                # Deliberately broad: every storage backend raises its own errors, and none of them
                # are actionable here. See the module docstring.
                logger.warning("could not delete %s for %s", name, label, exc_info=True)

    transaction.on_commit(_purge)
