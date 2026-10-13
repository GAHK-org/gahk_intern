"""Routes for Den Hurtige, mounted at /intern/den-hurtige/ (residents/urls.py).

Two rules govern the shape here:

  * The bare path must stay a real page. static/manifest.json uses /intern/den-hurtige/ as the
    PWA's `id` as well as its start_url, and a changed id makes every phone that already installed
    the app treat the next deploy as a *different* app. So it renders the default channel — not a
    redirect and not an index of channels.
  * `<slug:channel>/` is matched LAST, so the fixed segments below keep resolving. A channel slug
    equal to one of them would be shadowed with no error anywhere, so checks.E008 rejects that
    outright rather than leaving it to be discovered.
"""

from django.urls import path

from . import views

app_name = "den_hurtige"

urlpatterns = [
    path("", views.feed, name="feed"),
    path("opslag", views.feed_items, name="feed_items"),  # htmx poll target (partial, not a page)
    # The archive. A FIXED segment with the channel in the query string, matching feed_items rather
    # than the feed's own `<slug:channel>/` — the panel is fetched for whatever channel is on screen,
    # and a second path shape for the same choice is one more thing to keep in step. `?inden=`/`?id=`
    # carry the paging cursor; `?mere=1` asks for the next chunk rather than the whole panel.
    path("arkiv", views.archive, name="archive"),
    path("opret", views.create_post, name="create_post"),
    path("lyd/<slug:channel>", views.toggle_mute, name="toggle_mute"),
    path("<int:pk>/kommentar", views.create_comment, name="create_comment"),
    path("<int:pk>/traad", views.thread, name="thread"),
    path("<int:pk>/slet", views.delete_post, name="delete_post"),
    path("<int:pk>/reaktion", views.toggle_reaction, name="toggle_reaction"),
    # Where frontend/src/push.ts registers/removes a device (login-gated, CSRF-protected).
    path("abonner", views.save_subscription, name="save_subscription"),
    # Keep last: this pattern would otherwise swallow every fixed segment above.
    path("<slug:channel>/", views.feed, name="feed_channel"),
]
