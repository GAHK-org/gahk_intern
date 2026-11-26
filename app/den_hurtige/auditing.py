"""What the audit trail keeps of Den Hurtige.

A message, a reply and a deletion are recorded with what they said: the trail is the record of who
wrote what, also after the words have left the feed or been taken back. Reactions are left out.
"""

from audit import trail

from .models import QuickComment, QuickPost, QuickReaction


def _excerpt(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= 60 else f"{line[:59]}…"


trail.register(
    QuickPost,
    "den_hurtige.message",
    name=lambda post: f"{post.author.full_name}: {_excerpt(post.content) or '(slettet)'}",
    label="Besked i Den Hurtige",
    # When it was written: the event that wrote it says so.
    ignore={"created_at"},
    labels={
        "author": "Skrevet af",
        "content": "Tekst",
        "image": "Billede",
        "channel": "Kanal",
        "expires_at": "Forlader feedet",
        "deleted_at": "Slettet",
    },
)
trail.register(
    QuickComment,
    "den_hurtige.reply",
    name=lambda reply: f"{reply.author.full_name}: {_excerpt(reply.content) or '(billede)'}",
    label="Svar i Den Hurtige",
    ignore={"created_at"},
    labels={
        "post": "Besked",
        "author": "Skrevet af",
        "content": "Tekst",
        "image": "Billede",
        "notify_everyone": "Besked til alle",
    },
)
trail.exempt(QuickReaction, "an emoji on a message: left out of the trail by decision, like every reaction")

trail.actions(
    {
        "den_hurtige.message.posted": "Skrev en besked i Den Hurtige",
        "den_hurtige.message.deleted": "Slettede en besked i Den Hurtige",
        "den_hurtige.message.blanked": "Slettede en besked i Den Hurtige («Besked slettet» står tilbage)",
        "den_hurtige.reply.posted": "Svarede på en besked i Den Hurtige",
    }
)
