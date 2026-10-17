"""What every page needs for the bell and the device prompt."""

from django.http import HttpRequest

from core import push

from . import services


def notifications(request: HttpRequest) -> dict[str, object]:
    if not request.user.is_authenticated:
        return {}
    user_pk = request.user.pk
    return {
        # A callable, so the COUNT runs only on pages that render the badge (Django templates call
        # callables). Public CMS pages extend another base and never pay for it.
        "notifications_unseen": lambda: services.unseen_count(user_pk),
        "push_configured": push.is_configured(),
        "vapid_public_key": push.vapid_public_key(),
    }
