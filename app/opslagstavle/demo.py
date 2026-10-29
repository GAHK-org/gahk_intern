"""Demo content for opslagstavlen, used by `manage.py seed_demo`.

Lives here rather than inside seed_demo so the long Danish post bodies sit with the feature they
belong to, and so seed_demo keeps one line per domain.

Two of these posts exist for a reason beyond looking realistic:

  * one is **pinned**, so the pinned-first layout, the 📌 marker and the pin cap are all visible on a
    fresh checkout rather than only after someone thinks to pin something;
  * one carries **three images**, so the feed's collapse-to-a-thumbnail behaviour is visible too;
  * one is **years old**, because the board has no retention: the demo should show that a genuinely
    old opslag is still sitting there, still rendering and still paginating, rather than that
    something is about to remove it.
  * two embed a photo album and a GAHKFlix film (`seed_embeds`, run after those are seeded), so
    their widgets are visible too — one of them with no text around the album at all.

No *uploaded* images: this project has no Pillow, and `_seed_room_conditions` sets no photos for the
same reason. The multi-image post references files already in `static/legacy/` instead.
"""

import random
from datetime import datetime, timedelta

from residents.models import Resident, Role

from .models import Category, Notice, NoticeComment, NoticeReaction

VAERELSESRUNDE_BODY = """Runden er afsluttet. Fordelingen blev:

| Værelse | Beboer |
|---|---|
| 003 | Bo Beboer |
| 104 | Ann Anden |
| 210 | Carl Christensen |

Spørgsmål til **Indstillingen**."""

FAELLESSPISNING_BODY = """Vi mødes i spisesalen kl. 18.

- Tilmelding på sedlen i køkkenet
- 40 kr. pr. person
- Tag gerne en ven med

> Husk at melde fra senest torsdag hvis du ikke kan."""

# Several images, so a fresh checkout shows the feed's collapse-to-a-thumbnail behaviour without
# anyone having to upload anything. Static files rather than uploads because this project has no
# Pillow and the seed writes no media — /static/ images render and count exactly like uploads do
# (core.markdown.image_sources allows both origins).
ETAGEPLANER_BODY = """Planerne over etagerne hænger nu også her:

![Stuen](/static/legacy/image/intern/stuen.png)

![1. sal](/static/legacy/image/intern/sal1.png)

![2. sal](/static/legacy/image/intern/sal2.png)

Skriv en kommentar hvis noget mangler."""

CYKELKAELDER_BODY = """## Kort version

Cykler skal mærkes med værelsesnummer.

Umærkede cykler bliver fjernet efter **1. oktober**. Reglerne står på
[gahk.dk](https://gahk.dk), og spørgsmål kan stilles i kommentarerne."""

# (category, body). No headline any more -- the author heads the card -- so the posts that used to
# lead with one now open with a bold line the way a resident would actually write it.
POSTS = [
    (Category.VAERELSESRUNDE, VAERELSESRUNDE_BODY),
    (Category.BEGIVENHED, FAELLESSPISNING_BODY),
    (
        Category.FOEDSELSDAG,
        "**Tillykke til Ann!** Hun fylder 25 i dag — der er kage i køkkenet kl. 15.",
    ),
    (
        Category.PRAKTISK,
        "**Vaskemaskine 2 er i stykker.** Reparatøren kommer på torsdag; brug maskine 1 og 3 indtil da.",
    ),
    (Category.NYT, CYKELKAELDER_BODY),
    (Category.PRAKTISK, ETAGEPLANER_BODY),
    (
        Category.ANDET,
        (
            "**Sofa søger nyt hjem.** Den står i kælderen og skal væk inden weekenden — skriv en "
            "kommentar hvis du vil have den."
        ),
    ),
]

COMMENTS = ["Godt initiativ!", "Jeg er med.", "Kan man tage en gæst med?", "Tak for info."]
EMOJI = ["👍", "🎉", "❤️", "👀"]

# One deliberately ancient post. It used to exist so `purge_notices --dry-run` had something to
# report; nothing deletes posts any more, so it now demonstrates the opposite — that the board keeps
# its archive, and that a three-year-old opslag still renders and still paginates correctly.
STALE_AGE_DAYS = 365 * 3


def seed(residents: list[Resident], now: datetime, rng: random.Random) -> int:
    """Create the demo board. Returns the number of notices made."""
    created: list[Notice] = []
    for i, (category, body) in enumerate(POSTS):
        notice = Notice.objects.create(author=residents[i % len(residents)], category=category, body=body)
        # created_at is auto_now_add, so backdating takes a second write.
        Notice.objects.filter(pk=notice.pk).update(created_at=now - timedelta(days=rng.randint(1, 120)))
        created.append(notice)

    # Pinned by an actual inspektion member, so the demo shows a real attribution rather than "None".
    pinner = Resident.objects.filter(role_assignments__role=Role.INSPEKTION).first() or residents[0]
    Notice.objects.filter(pk=created[0].pk).update(pinned_at=now, pinned_by=pinner)

    stale = Notice.objects.create(
        author=residents[0],
        category=Category.ANDET,
        body="**Arkiveret opslag.** Gammelt nok til at purge_notices vil slette det. Findes for at "
        "gøre --dry-run synligt.",
    )
    Notice.objects.filter(pk=stale.pk).update(created_at=now - timedelta(days=STALE_AGE_DAYS))

    for notice in created[:4]:
        for resident in rng.sample(residents, k=min(3, len(residents))):
            NoticeComment.objects.create(notice=notice, author=resident, body=rng.choice(COMMENTS))
        # One reaction per person per notice is a DB constraint, so sample without replacement.
        for resident in rng.sample(residents, k=min(5, len(residents))):
            NoticeReaction.objects.create(notice=notice, author=resident, emoji=rng.choice(EMOJI))

    return len(created) + 1


def seed_embeds(residents: list[Resident], now: datetime) -> int:
    """Posts embedding a demo album and a demo film, drawn as their widgets (opslagstavle.embeds).

    Separate from `seed` because it needs the photo album and GAHKFlix seeded first; seed_demo runs
    it after both. Returns the number of notices made.
    """
    from core.embeds import markdown
    from gahkflix.models import Video
    from photo_album.models import Album

    bodies = []
    if (film := Video.objects.published().filter(title="Revy").first()) is not None:
        bodies.append(
            "Årets revy kan nu ses på GAHKFlix. Tak til alle, der var med! 🎭\n\n"
            + markdown("video", film.pk, film.title)
        )
    if (album := Album.objects.filter(parent__isnull=True, name="Sommerfest").first()) is not None:
        bodies.append(markdown("album", album.pk, album.name))
    for i, body in enumerate(bodies):
        notice = Notice.objects.create(author=residents[i % len(residents)], category=Category.NYT, body=body)
        Notice.objects.filter(pk=notice.pk).update(created_at=now - timedelta(hours=2 + i))
    return len(bodies)
