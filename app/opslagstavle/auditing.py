"""What the audit trail keeps of opslagstavlen: posts, comments, pinning and uploaded pictures."""

from audit import trail

from .models import Notice, NoticeComment, NoticeImage, NoticeReaction


def _excerpt(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= 60 else f"{line[:59]}…"


trail.register(
    Notice,
    "opslagstavle.notice",
    name=lambda notice: f"{notice.author.full_name}: {_excerpt(notice.body)}",
    label="Opslag",
    # When it was written and last edited: the events that did it say so (edited_at is the "Redigeret"
    # stamp the edit view sets).
    ignore={"created_at", "edited_at"},
    labels={
        "author": "Skrevet af",
        "author_embedsgruppe": "Embedsgruppe",
        "pinned_at": "Fastgjort",
        "pinned_by": "Fastgjort af",
    },
)
trail.register(
    NoticeComment,
    "opslagstavle.comment",
    name=lambda comment: f"{comment.author.full_name}: {_excerpt(comment.body) or '(billede)'}",
    label="Kommentar på opslagstavlen",
    ignore={"created_at"},
    labels={"notice": "Opslag", "author": "Skrevet af", "author_embedsgruppe": "Embedsgruppe"},
)
trail.register(
    NoticeImage,
    "opslagstavle.image",
    name=str,
    label="Billede på opslagstavlen",
    # Which post holds it follows the post's text, which is recorded on the post: saving a post
    # claims the pictures it shows and lets go of the rest.
    ignore={"uploaded_at", "notice"},
    labels={"uploaded_by": "Uploadet af"},
)
trail.exempt(NoticeReaction, "an emoji on a post: left out of the trail by decision, like every reaction")

trail.actions(
    {
        "opslagstavle.notice.posted": "Slog et opslag op",
        "opslagstavle.notice.edited": "Rettede opslaget",
        "opslagstavle.notice.deleted": "Slettede opslaget",
        "opslagstavle.notice.pinned": "Fastgjorde opslaget",
        "opslagstavle.notice.unpinned": "Frigjorde opslaget",
        "opslagstavle.comment.posted": "Kommenterede et opslag",
        "opslagstavle.comment.deleted": "Slettede en kommentar på opslagstavlen",
        "opslagstavle.image.uploaded": "Uploadede et billede til et opslag",
        "opslagstavle.image.purged": "Ryddede billeder, som intet opslag bruger",
    }
)
