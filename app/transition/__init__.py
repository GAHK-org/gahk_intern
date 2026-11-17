"""TEMPORARY: what intern does between deploying the new code and moving gahk.dk to its webhost.

DEPLOY.md, "Transition (temporary — removed in phase 3)", has the phases. In short: the new intern is
deployed (phase 1) while gahk.dk's DNS still points at this server, so for a while this app answers
gahk.dk as well as intern.gahk.dk. Then the operator ends the old sessions and moves gahk.dk to
Hetzner (phase 2), and later this whole package is reverted (phase 3). Nothing here may outlive
that: no models, no migrations, and every hook outside this package sits in one marked place
(the settings block "Transition", the DEPLOY.md section, the tests in tests/test_transition.py).

Two jobs:

  * **The old public site for gahk.dk** (transition.middleware.PublicSiteHost, transition.urls_public,
    transition.views): for the hosts in settings.TRANSITION_PUBLIC_HOSTS, the site the old code
    showed — front page, pages, news and events, the admission forms — rendered from today's
    content; and everything else at those hosts as gahk.dk will answer it after the DNS switch, so
    nobody notices the switch.
  * **Logins carried over** (transition.middleware.CarryOverLegacySession, the command
    `end_legacy_sessions`): a browser that logged in to the old code holds a ".gahk.dk" `sessionid`;
    on intern.gahk.dk its session is moved to a new key under the new host-only cookie instead of
    the user being logged out. Every session the new code saves is marked, and in phase 2 the
    command ends every unmarked one, because from then on the old ".gahk.dk" cookie reaches the
    webhost.
"""
