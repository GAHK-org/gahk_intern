"""Tying uploaded images to the post that embeds them.

The problem: the compose toolbar uploads an image and inserts `![alt](/media/opslag/…)` into a
textarea, so the *reference* lives inside Markdown text, not in a foreign key — and the post may not
exist yet when the upload happens. Left alone, that means nothing ever knows which files are still
in use, and deleting a post leaves its pictures readable at their /media/ URLs forever (media is
public by URL in every environment; see config/urls.py).

The fix is to turn the textual reference back into a real FK at save time, by parsing the body.
Everything good follows from that:

  * Deleting a post deletes its images, as a *database* property (CASCADE + the post_delete receiver
    on NoticeImage) — including under a *bulk* queryset delete, which never calls Model.delete().
  * Editing a post to remove an image releases that row, and the nightly sweep collects it after a
    day's grace (so removing and immediately re-adding an image cannot lose the file).
  * An upload nobody ever referenced is `notice_id IS NULL` — one exact, indexed query.

The alternative — scanning every post body for each image's URL, the way CmsImageAdmin.usage does —
was rejected: it is O(images × posts) with a `LIKE '%…%'` no index can serve, and it can never be
exact, because a URL inside a fenced code block looks like a reference. That leaves a choice between
leaking files forever and occasionally deleting a live image. `core.markdown.extract_image_names`
walks the token stream instead, so a code sample is correctly not a reference.
"""

from django.db import transaction
from django.db.models import Q

from core.markdown import extract_image_names

from .models import Notice, NoticeImage


@transaction.atomic
def sync_images(notice: Notice) -> None:
    """Claim the images `notice` references, and release the ones it no longer does.

    Call after every create and every edit.
    """
    names = extract_image_names(notice.body)

    # Claim. Two filters, both load-bearing:
    #   uploaded_by=author — you cannot claim (and so cannot later destroy) somebody else's upload
    #                        by pasting its URL into your own post.
    #   unclaimed OR already ours — referencing another post's image never steals it. The picture
    #                        still renders; it just stays owned by the post that uploaded it.
    if names:
        NoticeImage.objects.filter(file__in=names, uploaded_by_id=notice.author_id).filter(
            Q(notice__isnull=True) | Q(notice=notice)
        ).update(notice=notice)

    # Release anything this post used to reference and no longer does. Scoped to `notice.images`, so
    # editing post B can never touch post A's pictures. The row is not deleted here — the sweep does
    # that a day later, which is what makes "removed it, then changed my mind" non-destructive.
    stale = notice.images.all()
    if names:
        stale = stale.exclude(file__in=names)
    stale.update(notice=None)
