"""The channel registry — Den Hurtige's topic-separated feeds.

Channels are **constants in code, not database rows**, matching the stance the CMS takes
(cms/models.py: content is version-controlled, not edited at runtime). The consequences are worth
spelling out, because they are the whole reason this file is a tuple instead of a model:

  * QuickPost.channel is a plain CharField, so adding channels needed one additive migration ever —
    not a nullable-FK / backfill / non-null dance per change.
  * The sidebar and the tab strip read this tuple, so neither costs a query on every page render.
  * Adding a channel is a two-line diff that goes through review and deploys like any other change,
    which for something that changes about once a year is the cheaper workflow.

The cost is that Inspektionen cannot add a channel without a deploy. If that ever becomes the
bottleneck, this module is the seam to replace: everything else asks it for a Channel by slug.

Because there is no DB constraint behind any of this, checks.py validates the tuple at startup
(E007-E009): unique slugs, no collision with a fixed URL segment, and a duration the composer can
actually offer.
"""

from dataclasses import dataclass

from django.urls import reverse

from .models import DURATION_CHOICES


@dataclass(frozen=True)
class Channel:
    """One feed. `icon` is a key into the SVG sprite defined in templates/base.html."""

    slug: str
    name: str
    icon: str
    description: str
    # Preselected in the composer's duration picker. Must be one of DURATION_CHOICES (checks.E009);
    # the author can still pick any of them.
    #
    # Per channel because "skal vi i byen om en time" and "hvem har set min cykel" go stale on very
    # different schedules -- though as of the move to a 2-døgn default every channel happens to
    # agree, GAHKroom included. The field stays per channel because that is a property of the
    # channels, not of the number they currently share; test_the_composer_offers_the_channels_own
    # _default_duration patches in a channel with a different one so the wiring stays covered while
    # the real values coincide.
    default_duration: int
    # None = everyone who can reach Den Hurtige at all (den_hurtige.access). A tuple restricts the
    # channel to those roles — the seam for a future "Inspektion internt" without another rollout
    # mechanism. Both launch channels are open.
    roles: tuple[str, ...] | None = None

    @property
    def url(self) -> str:
        """This channel's canonical URL.

        The default channel answers on the bare /intern/den-hurtige/ rather than on its own slug,
        because static/manifest.json uses that path as the PWA's `id`. A manifest id that changes
        makes every phone treat the next deploy as a *different* installed app, so the bare URL has
        to keep rendering a real feed forever. (/generelt/ also resolves; this is the one to link.)
        """
        if self.slug == DEFAULT.slug:
            return reverse("den_hurtige:feed")
        return reverse("den_hurtige:feed_channel", args=[self.slug])


CHANNELS: tuple[Channel, ...] = (
    Channel(
        slug="generelt",
        name="Den Hurtige",
        icon="flash",
        description="Alt det korte og hurtige.",
        default_duration=2880,
    ),
    Channel(
        slug="tv-rezz",
        name="TV-Rezz",
        icon="tv",
        description="TV-Rezz.",
        default_duration=2880,
    ),
    Channel(
        slug="sportsmann",
        name="G. A. Sportsmann",
        icon="ball",
        description="G. A. Sportsmann.",
        default_duration=2880,
    ),
    Channel(
        slug="gahkroom",
        name="GAHKroom",
        icon="beer",
        description="Alle er fucking liderlige og rowdy herinde",
        default_duration=2880,
    ),
    Channel(
        slug="mhga",
        name="M.H.G.A",
        icon="users",
        description="Make Hallen Great Again.",
        default_duration=2880,
    ),
)

# The channel a post lands in when nothing says otherwise, and the one the bare URL renders. Must
# match the `default=` on QuickPost.channel — checks.py asserts they agree (E010), because a
# mismatch would file every legacy row into a channel no tab links to.
#
# Its slug stays the neutral "generelt" even though it is named "Den Hurtige": the canonical URL for
# the default channel is the bare /intern/den-hurtige/, so a matching slug would only ever surface
# as the alias /intern/den-hurtige/den-hurtige/. Slug and name are free to differ; only the slug
# is stored on posts.
DEFAULT = CHANNELS[0]

BY_SLUG: dict[str, Channel] = {c.slug: c for c in CHANNELS}

# URL segments under /intern/den-hurtige/ that are views, not channels. `<slug:channel>/` is
# matched last, so these already win — but a channel named after one would be unreachable with no
# error anywhere, so checks.py rejects the collision instead (E008).
RESERVED_SLUGS = frozenset({"opslag", "opret", "abonner", "lyd", "arkiv"})

VALID_DURATIONS = {minutes for minutes, _label in DURATION_CHOICES}


def lookup(slug: str | None) -> Channel | None:
    """The channel for a slug, or None if there is no such channel.

    Returns None rather than raising so each caller can fail in the way its own surface needs: the
    page 404s, while the htmx poll answers 204 (see views.feed_items — an error body swapped into
    the middle of the feed is worse than nothing).
    """
    if not slug:
        return DEFAULT
    return BY_SLUG.get(slug)


def visible(roles: set[str] | frozenset[str]) -> list[Channel]:
    """The channels a role set may see, in registry order — what the tab strip renders."""
    return [c for c in CHANNELS if allowed(c, roles)]


def allowed(channel: Channel, roles: set[str] | frozenset[str]) -> bool:
    """Whether a role set may open this channel. Mirrors access.roles_allowed, and stacks with it:
    den_hurtige.access gates the feature, this gates one channel within it."""
    return channel.roles is None or not set(roles).isdisjoint(channel.roles)
