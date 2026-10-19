"""What begivenheder plugs into other domains. Imported once, from EventsConfig.ready().

Today that is the member dashboard's "Begivenheder på GAHK" card. The dashboard is identity's page,
and identity sits below every feature (tach.toml layers), so it cannot import this app to draw the
card; it renders whatever residents.api.register_dashboard_card was given instead.
"""

from django.http import HttpRequest
from django.template.loader import render_to_string
from django.utils.safestring import SafeString

from residents.api import current_resident, register_dashboard_card

from .access import request_allowed
from .agenda import agenda


def agenda_card(request: HttpRequest) -> SafeString:
    """The kollegium's own calendar, for every reader — with the agenda itself only for one the
    rollout gate lets in. Anyone else gets the card saying begivenheder is not open to them yet.

    Rendered without the request: the card needs no context processor, and with one it would run
    every one of them (the sidebar's gates, the bell) a second time for the same page.
    """
    allowed = request_allowed(request)
    return render_to_string(
        "events/_dashboard_agenda.html", {"agenda": agenda(current_resident(request)) if allowed else None}
    )


register_dashboard_card(agenda_card)
