// Service worker for the intern PWA: every push notification on the site lands here.
//
// Served from the ROOT path (/sw.js, see config/urls.py) so its scope covers /intern/. It lives
// under templates/ because a TemplateView renders it, but the contents are deliberately plain
// JavaScript with no Django tags at all: editors lint this file as JS, and any tag syntax here —
// even inside a comment — is parsed by the template engine and can 500 the response.
// Icon URLs are therefore the unhashed /static/ paths, exactly as in static/manifest.json;
// collectstatic keeps the originals next to the hashed copies, so both resolve in production.
//
// This is the ONLY service worker on the site, so every push subscription belongs to this
// registration and every push event lands here. Keep it that way: a second worker registered from
// a subdirectory would take ownership of subscriptions made under its scope, and those pushes would
// silently stop arriving in this file.

// Bump when editing this file. A service worker is cached aggressively and updates silently, so
// without a version marker there is no way to tell which copy is actually running — and debugging
// "no notification arrives" against a stale worker wastes a lot of time.
var SW_VERSION = 5;

// Take over immediately instead of waiting for every tab to close — otherwise a fixed push handler
// only reaches users after they close all intern tabs.
self.addEventListener('install', function (event) {
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  console.log('[sw] GAHK intern service worker v' + SW_VERSION + ' active');
  event.waitUntil(self.clients.claim());
});

// A no-op fetch handler. Nothing is cached (the feed must never be stale), but having a fetch
// listener at all is what several browsers look for before offering to install the app.
self.addEventListener('fetch', function (event) {});

// Incoming push from core/push.py (built by notifications/services.py). Payload keys:
//   head, body, icon  what the notification says.
//   url               where the notification points (the post, the event, the feed).
//   open              where a TAP goes: /intern/notifikationer/aabn/<batch>/, which marks the
//                     resident's in-app row seen and then redirects to `url`. Falls back to `url`.
//   tag               set for grouped notifications ("A og B reagerede"): the new one replaces the
//                     previous one for the same thing instead of stacking. Ungrouped ones have no
//                     tag, so a second post never silently overwrites the first.
//   vibrate           false = deliver silently. Decided server-side from the resident's settings.
//   importance        1-4; 4 (Kritisk) stays on screen until dismissed.
// `head` is the sender's name or a short title, not the feature name: iOS and Android already
// label the notification with the app it came from. The fallbacks only fire for a payload-less push.
self.addEventListener('push', function (event) {
  var data = {};
  if (event.data) {
    try {
      data = event.data.json();
    } catch (err) {
      data = { body: event.data.text() };
    }
  }
  // Logged so "the push never arrived" and "the push arrived but nothing was displayed" can be told
  // apart — the second is an OS/browser notification setting, the first is delivery.
  console.log('[sw] push received', data);

  var vibrate = data.vibrate !== false;
  var options = {
    body: data.body || 'Åbn for at læse mere.',
    icon: data.icon || '/static/icons/icon-192x192.png',
    badge: '/static/icons/badge-72x72.png', // monochrome, for the Android status bar
    silent: !vibrate,
    requireInteraction: data.importance === 4,
    data: { url: data.open || data.url || '/intern/notifikationer/' },
  };
  if (vibrate) {
    options.vibrate = data.importance >= 3 ? [200, 100, 200, 100, 200] : [200, 100, 200];
  }
  if (data.tag) {
    options.tag = data.tag;
    // Without renotify a replaced notification updates in place without a sound or buzz, so the
    // second like on a post would be invisible until the shade is opened.
    options.renotify = vibrate;
  }

  event.waitUntil(
    self.registration.showNotification(data.head || 'Ny besked', options).then(
      function () {
        console.log('[sw] notification shown');
      },
      function (err) {
        // showNotification rejects if the icon/badge cannot be fetched, among other things.
        console.error('[sw] showNotification failed', err);
      },
    ),
  );
});

// Focus an intern tab that is already open before opening a new one — tapping three notifications
// should not leave three copies of the feed behind.
self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  var target = (event.notification.data && event.notification.data.url) || '/intern/notifikationer/';

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (windows) {
      for (var i = 0; i < windows.length; i++) {
        if (new URL(windows[i].url).pathname.indexOf('/intern/') === 0 && 'focus' in windows[i]) {
          return windows[i].navigate ? windows[i].navigate(target).then(function (c) { return c.focus(); })
                                     : windows[i].focus();
        }
      }
      return self.clients.openWindow(target);
    }),
  );
});
