"""Demo conversations for Den Hurtige, used by ``manage.py seed_demo``."""

import random
from datetime import datetime, timedelta
from io import BytesIO

from django.core.files.base import ContentFile

from residents.models import Resident

from .models import DEFAULT_CHANNEL_SLUG, QuickComment, QuickPost, QuickReaction

POSTS = [
    # channel, message age, expiry relative to now, replies, post image, reply image, reactions
    (
        "generelt",
        "Er der nogen hjemme til at tage imod en pakke i eftermiddag?",
        timedelta(minutes=4),
        timedelta(hours=12),
        2,
        False,
        False,
        3,
    ),
    (
        "generelt",
        "Nogen der har set min nøgle med den røde snor?",
        timedelta(minutes=12),
        timedelta(hours=13),
        2,
        True,
        True,
        4,
    ),
    (
        "generelt",
        "Jeg har lavet kaffe i fælleskøkkenet.",
        timedelta(minutes=24),
        timedelta(hours=14),
        0,
        False,
        False,
        0,
    ),
    (
        "generelt",
        "Er vaskemaskine 2 ledig om en times tid?",
        timedelta(minutes=45),
        timedelta(hours=15),
        1,
        False,
        False,
        1,
    ),
    (
        "generelt",
        "Der står en cykel uden lys ved porten. Er den nogens?",
        timedelta(hours=2),
        timedelta(hours=16),
        0,
        True,
        False,
        0,
    ),
    (
        "generelt",
        "Vi løber en rolig tur fra porten kl. 17.30.",
        timedelta(hours=5),
        timedelta(hours=17),
        2,
        False,
        False,
        2,
    ),
    (
        "generelt",
        "Har nogen et ekstra lagen, jeg kan låne til i morgen?",
        timedelta(hours=11),
        timedelta(hours=18),
        1,
        False,
        False,
        0,
    ),
    (
        "generelt",
        "Der er rester af grøn karry i køleskabet.",
        timedelta(days=1, hours=3),
        timedelta(hours=19),
        0,
        True,
        False,
        2,
    ),
    (
        "generelt",
        "Hallen trænger til en hurtig oprydning før weekendens gæster.",
        timedelta(days=1, hours=10),
        timedelta(hours=20),
        2,
        False,
        True,
        0,
    ),
    (
        "generelt",
        "Er der nogen, der vil se kamp kl. 20 i fællesrummet?",
        timedelta(days=2),
        timedelta(hours=21),
        1,
        False,
        False,
        4,
    ),
    (
        "generelt",
        "Er den sorte boremaskine stadig hos nogen?",
        timedelta(days=5),
        timedelta(days=-2),
        3,
        False,
        False,
        3,
    ),
    (
        "generelt",
        "Nogen der ved, hvem der har fællesrumsnøglen?",
        timedelta(days=18),
        timedelta(days=-12),
        0,
        False,
        False,
        0,
    ),
    (
        "generelt",
        "Fik nogen hentet pakken fra postkassen i sidste uge?",
        timedelta(days=45),
        timedelta(days=-40),
        1,
        True,
        False,
        1,
    ),
    (
        "sportsmann",
        "Vi mangler én til rundbold på søndag.",
        timedelta(hours=3),
        timedelta(hours=10),
        1,
        False,
        False,
        2,
    ),
    ("sportsmann", "Tak for en god løbetur i går.", timedelta(days=4), timedelta(days=-1), 0, True, False, 0),
    (
        "gahkroom",
        "Er der plads til to mere ved bordet?",
        timedelta(minutes=35),
        timedelta(hours=8),
        0,
        False,
        False,
        1,
    ),
    ("gahkroom", "Her er billedet fra i fredags.", timedelta(days=7), timedelta(days=-3), 2, True, True, 4),
    (
        "mhga",
        "Der står nye stole ved Hallen, som skal ind før regnen.",
        timedelta(hours=7),
        timedelta(hours=6),
        1,
        False,
        False,
        0,
    ),
    (
        "mhga",
        "Oprydningen er klaret, tak for hjælpen.",
        timedelta(days=9),
        timedelta(days=-5),
        0,
        False,
        False,
        2,
    ),
]
COMMENTS = ["Jeg kan godt.", "Jeg er på vej hjem nu.", "Tak, det løser jeg.", "Jeg har set den ved sofaen."]
EMOJI = ["👍", "🎉", "👀", "❤️", "✅"]


def seed(residents: list[Resident], now: datetime, rng: random.Random) -> int:
    """Create a varied message matrix; TV-Rezz is deliberately left empty."""
    if not residents:
        return 0

    made = 0
    for index, (
        channel,
        content,
        age,
        expires_in,
        reply_count,
        has_image,
        reply_has_image,
        reaction_count,
    ) in enumerate(POSTS):
        post = QuickPost.objects.create(
            author=residents[index % len(residents)],
            channel=channel,
            content=content,
            image=ContentFile(_image(content, index), name=f"hurtig-{index + 1}.jpg") if has_image else "",
            expires_at=now + expires_in,
        )
        created_at = now - age
        QuickPost.objects.filter(pk=post.pk).update(created_at=created_at)
        for reply_index, resident in enumerate(residents[index + 1 : index + 1 + reply_count]):
            comment = QuickComment.objects.create(
                post=post,
                author=resident,
                content=COMMENTS[(index + reply_index) % len(COMMENTS)],
                image=(
                    ContentFile(
                        _image("Svar", index + reply_index), name=f"hurtig-svar-{index}-{reply_index}.jpg"
                    )
                    if reply_has_image and reply_index == reply_count - 1
                    else ""
                ),
                notify_everyone=reply_index == 0,
            )
            QuickComment.objects.filter(pk=comment.pk).update(
                created_at=created_at + timedelta(minutes=reply_index + 1)
            )
        for resident in rng.sample(residents, k=min(reaction_count, len(residents))):
            QuickReaction.objects.create(post=post, author=resident, emoji=rng.choice(EMOJI))
        made += 1

    # One tombstone, with replies -- awkward to reach by hand, since deleting a message you have
    # just posted takes the silent path until DELETE_GRACE has passed. Via soft_delete() rather than
    # by writing `deleted_at`, so a break in the real path shows up here.
    if made:
        doomed = QuickPost.objects.create(
            author=residents[0],
            channel=DEFAULT_CHANNEL_SLUG,
            content="Beskeden her er slettet igen af den der skrev den.",
            expires_at=now + timedelta(hours=6),
        )
        QuickPost.objects.filter(pk=doomed.pk).update(created_at=now - timedelta(hours=1))
        for reply_index, resident in enumerate(residents[1:3]):
            reply = QuickComment.objects.create(
                post=doomed, author=resident, content=COMMENTS[reply_index % len(COMMENTS)]
            )
            QuickComment.objects.filter(pk=reply.pk).update(
                created_at=now - timedelta(hours=1) + timedelta(minutes=reply_index + 1)
            )
        doomed.refresh_from_db()
        doomed.soft_delete()
        made += 1
    return made


def _image(label: str, index: int) -> bytes:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return label.encode()
    image = Image.new("RGB", (1000, 700), ["#315b8d", "#1f6c64", "#8a4e70"][index % 3])
    ImageDraw.Draw(image).text((40, 40), label, fill="white")
    output = BytesIO()
    image.save(output, format="JPEG", quality=82)
    return output.getvalue()
