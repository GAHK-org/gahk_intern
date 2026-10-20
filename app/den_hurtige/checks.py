"""Startup validation of the channel registry — constraints a DB table would give for free.

The registry (den_hurtige.channels) is a tuple in code rather than table rows, so it has no unique
constraint, no FK and no `choices=` behind it. These checks are what replaces them:

  E007  two channels share a slug           — the later one is unreachable; BY_SLUG silently keeps one
  E008  a slug collides with a URL segment  — urls.py matches the fixed path first, so the channel
        never resolves and nothing anywhere reports it
  E010  channels.DEFAULT disagrees with QuickPost.channel's field default — every row written before
        the channel field existed would sit in a channel no tab links to

The VAPID key pair used to be validated here too; it moved to core.checks with the rest of the push
stack, and its IDs are now core.E001-E006.
"""

from collections.abc import Sequence

from django.apps.config import AppConfig
from django.core.checks import CheckMessage, Error


def check_channels(app_configs: Sequence[AppConfig] | None, **kwargs: object) -> list[CheckMessage]:
    """Validate the channel registry — the constraints a DB table would have enforced for free."""
    from . import channels
    from .models import DEFAULT_CHANNEL_SLUG

    errors: list[CheckMessage] = []

    seen: set[str] = set()
    for channel in channels.CHANNELS:
        if channel.slug in seen:
            errors.append(
                Error(
                    f"Two channels share the slug {channel.slug!r}.",
                    hint="Slugs are the URL and the value stored on every post; they must be unique.",
                    id="den_hurtige.E007",
                )
            )
        seen.add(channel.slug)

        if channel.slug in channels.RESERVED_SLUGS:
            errors.append(
                Error(
                    f"Channel slug {channel.slug!r} collides with a fixed URL segment.",
                    hint=(
                        "den_hurtige/urls.py matches that path before <slug:channel>/, so the "
                        f"channel would never open. Reserved: {sorted(channels.RESERVED_SLUGS)}."
                    ),
                    id="den_hurtige.E008",
                )
            )

    if channels.DEFAULT.slug != DEFAULT_CHANNEL_SLUG:
        errors.append(
            Error(
                f"channels.DEFAULT is {channels.DEFAULT.slug!r} but QuickPost.channel defaults to "
                f"{DEFAULT_CHANNEL_SLUG!r}.",
                hint=(
                    "Every post written before the channel field existed carries the model default. "
                    "If the two disagree, those posts sit in a channel no tab links to."
                ),
                id="den_hurtige.E010",
            )
        )

    return errors
