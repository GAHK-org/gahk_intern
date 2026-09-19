"""Django settings for the GAHK rewrite (config project).

Schema/decisions: see ../02-schema-etl.md. Target DB is PostgreSQL (via DATABASE_URL).
"""

import os
from pathlib import Path

import dj_database_url
from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

# Behind Coolify/Traefik, TLS is terminated at the proxy and plain HTTP is forwarded to gunicorn.
# Trust the forwarded-proto header so request.is_secure(), CSRF, and secure cookies see HTTPS —
# without this, every form POST (login included) fails CSRF in prod.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Full https origins that may POST (scheme required, comma-separated), e.g.
# "https://gahk.dk,https://www.gahk.dk". Set in the environment for prod.
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o]
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_celery_beat",
    # GAHK domains
    "core",
    "residents",
    "admissions",
    "cms",
    "ak",
    "rooms",
    "oelkaelder",
    "stats",
    "den_hurtige",
    "opslagstavle",
    "events",
    "reparationer",
    "arkiv",
    "photo_album",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "stats.middleware.FrontPageVisitCounterMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.navigation",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    if DEBUG:
        DATABASE_URL = "postgres://gahk:gahk@localhost:5432/gahk"
    else:
        raise ImproperlyConfigured("DATABASE_URL must be configured outside development.")

DATABASES = {"default": dj_database_url.config(default=DATABASE_URL, conn_max_age=600)}

# --- Auth (01-infrastructure.md A4/A5; 02-schema-etl.md §1.6) ---
AUTH_USER_MODEL = "residents.Resident"

# First hasher = default for new/upgraded passwords. The legacy hasher (last) only verifies the old
# unsalted sha256 hashes, then Django re-hashes on next login (upgrade-on-login, scope §5).
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
    "core.hashers.GahkLegacySHA256PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Password-reset links expire after 2 hours (F-014 decision, 2026-06).
PASSWORD_RESET_TIMEOUT = 7200

LANGUAGE_CODE = "da"
TIME_ZONE = "Europe/Copenhagen"
USE_I18N = True
USE_TZ = True

# Celery persists queued messages and task results in PostgreSQL. Keeping this separate allows a
# dedicated queue database later, while local and production defaults share the Django database.
CELERY_DATABASE_URL = os.environ.get("CELERY_DATABASE_URL", DATABASE_URL)
# Django accepts `postgres://`; SQLAlchemy requires the explicit PostgreSQL dialect and driver.
if CELERY_DATABASE_URL.startswith("postgres://"):
    CELERY_DATABASE_URL = CELERY_DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif CELERY_DATABASE_URL.startswith("postgresql://"):
    CELERY_DATABASE_URL = CELERY_DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", f"sqla+{CELERY_DATABASE_URL}")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", f"db+{CELERY_DATABASE_URL}")
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_TIMEZONE = TIME_ZONE
# Keep work on the broker until it completes. If a worker process is killed, Celery rejects the
# unacknowledged delivery so it can be picked up when a worker is available again.
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
# Without this, Celery stores only status/result/traceback and leaves `name`, `worker`, `queue` and
# `retries` NULL in celery_taskmeta — which are four of the columns the siteadmin worker-jobs page
# selects and displays, so "Worker", "Kø" and "Forsøg" were permanently blank there.
CELERY_RESULT_EXTENDED = True
# The default ceiling for a scheduled job: generous for a sweep, and short enough that a wedged one
# does not hold a worker slot all night. The two photo-album jobs legitimately run longer and set
# their own limits at the task — see photo_album/tasks.py::MEDIA_TASK_TIME_LIMIT.
#
# Redelivery from the two settings above does NOT make the reapers redundant: `build_album_download`
# and `process_album_import` both refuse to run unless their row is still QUEUED, and by the time a
# worker is lost it is BUILDING — so the redelivered message returns False and the row stays wedged.
# What rescues those is fail_stalled_downloads; what rescues a media row is its claim expiring.
CELERY_TASK_TIME_LIMIT = 900

# A local migration client uses this bearer token to submit album ZIP imports as the non-login
# System resident. Leave blank to disable token-authenticated imports.
PHOTO_ALBUM_IMPORT_TOKEN = os.environ.get("PHOTO_ALBUM_IMPORT_TOKEN", "")
PHOTO_ALBUM_SYSTEM_IMPORT_EMAIL = os.environ.get("PHOTO_ALBUM_SYSTEM_IMPORT_EMAIL", "system@gahk.dk")
CELERY_BEAT_SCHEDULE = {
    "purge-expired-applications": {
        "task": "admissions.tasks.purge_expired_applications",
        "schedule": crontab(minute=20, hour=3),
    },
    "purge-orphaned-notice-images": {
        "task": "opslagstavle.tasks.purge_orphaned_images",
        "schedule": crontab(minute=40, hour=3),
    },
    "archive-finished-repairs": {
        "task": "reparationer.tasks.archive_finished_repairs",
        "schedule": crontab(minute=50, hour=3),
    },
    "purge-expired-events": {
        "task": "events.tasks.purge_expired_events",
        "schedule": crontab(minute=0, hour=4),
    },
    "purge-expired-photo-album-downloads": {
        "task": "photo_album.tasks.purge_expired_downloads",
        "schedule": crontab(minute=20, hour=4),
    },
    # 04:30, not 04:10: at 04:10 it ran on top of `apply-ak-monthly-assessment` on the 1st of every
    # month. Every job here is staggered so two never run together on the one small box, and this
    # was the one pair that wasn't. (The collision is older than Celery — the cron table in
    # DEPLOY.md §4b had `purge_photo_album` and `ak_monthly_assessment` both at 04:10 — so it was
    # carried over rather than introduced, but it is fixed here rather than carried further.)
    "purge-expired-photo-album-media": {
        "task": "photo_album.tasks.purge_expired_media",
        "schedule": crontab(minute=30, hour=4),
    },
    "apply-ak-monthly-assessment": {
        "task": "ak.tasks.apply_monthly_assessment",
        "schedule": crontab(minute=10, hour=4, day_of_month=1),
    },
    # Hourly, and deliberately not daily: what it clears is a download the resident is still
    # watching a spinner for, so the gap between "the worker died" and "the page says so" is the
    # thing being minimised.
    "fail-stalled-photo-album-downloads": {
        "task": "photo_album.tasks.fail_stalled_downloads",
        "schedule": crontab(minute=5),
    },
    # 04:50, after every other sweep: the rows it deletes are the messages those sweeps were
    # delivered on, so running it last keeps one night's work visible in the table while that work
    # is still happening.
    "purge-delivered-broker-messages": {
        "task": "core.tasks.purge_delivered_broker_messages",
        "schedule": crontab(minute=50, hour=4),
    },
    "process-photo-album-media": {
        "task": "photo_album.tasks.process_pending_media",
        # Uploads enqueue their own derivative build on commit. This is only the nightly recovery
        # pass for broker messages lost while unavailable or work stranded by a killed worker.
        "schedule": crontab(minute=0, hour=2),
    },
    "remind-rsvp-deadlines": {
        "task": "events.tasks.remind_rsvp_deadlines",
        "schedule": crontab(minute=0, hour=17),
    },
    "email-oelkaelder-monthly-statements": {
        "task": "oelkaelder.tasks.send_monthly_statements",
        "schedule": crontab(minute=10, hour=6, day_of_month=1),
    },
    "send-admin-dummy-notification": {
        "task": "core.tasks.send_admin_dummy_notification",
        "schedule": crontab(minute=0, hour="8,16"),
    },
}

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]  # holds the Vite-built bundle (static/dist/)

# Media (user uploads) always go to object storage: Hetzner in production, MinIO for local dev
# (docker-compose.yml's `minio` service) — there is no local-disk fallback. STORAGES["default"]
# below is unconditionally core.storage.MediaS3Storage, so a missing or wrong S3_BUCKET is a loud
# S3 error on the first upload, not a silent switch to writing files onto a disk nobody backs up.
#
# The one place local disk survives is the test suite: tests/conftest.py's autouse fixture
# overrides STORAGES["default"] to plain FileSystemStorage for every test, so pytest never touches
# a real bucket. That override is test-only — nothing here grants the same thing anywhere else.
#
# MEDIA_URL stays "/media/" regardless: it is a prefix of content stored in the database, and
# core.checks (core.E007-E009) refuses to start the process if it or the backend's URLs ever stop
# matching. core/storage.py has the full argument.
#
# Defaults describe the MinIO container docker-compose.yml runs for local dev (bucket, credentials,
# endpoint, path-style addressing), not Hetzner — so S3 works out of the box on a fresh checkout with
# no app/.env at all. Gated on DEBUG rather than unconditional: DEPLOY.md requires DJANGO_DEBUG=0 in
# production, so these defaults can never be what a real deploy silently runs on.
S3_BUCKET = os.environ.get("S3_BUCKET", "gahk-s3" if DEBUG else "")
# fsn1 (Falkenstein) / nbg1 (Nuremberg) / hel1 (Helsinki). Keep this in the same location as the VM:
# traffic inside eu-central does not count against the account's egress allowance.
S3_LOCATION = os.environ.get("S3_LOCATION", "fsn1")

# Overridable for S3-compatible endpoints that are not Hetzner — namely the MinIO container
# docker-compose.yml runs for local dev. Path-style addressing is required there: MinIO has no
# wildcard TLS certificate for virtual-hosted-style requests, and unlike Hetzner it is reached over
# plain HTTP on the docker network.
S3_ENDPOINT_URL = os.environ.get(
    "S3_ENDPOINT_URL", "http://localhost:9000" if DEBUG else f"https://{S3_LOCATION}.your-objectstorage.com"
)
S3_ADDRESSING_STYLE = os.environ.get("S3_ADDRESSING_STYLE", "path" if DEBUG else "virtual")

# The endpoint a BROWSER can actually reach, for presigned URLs only — everything else (uploads,
# HEAD, delete, ...) keeps using S3_ENDPOINT_URL above, which this process itself resolves fine.
# The two differ only under `task dev` (the dockerized `web` service): Django reaches MinIO over the
# compose network at http://minio:9000, but the browser that follows the presigned URL is on the
# HOST, which can only reach MinIO's published port at http://localhost:9000. Signing the URL with
# the wrong host is not cosmetic — SigV4 signs the Host header, so the bucket 403s a request whose
# Host does not match the one it was signed for, and rewriting the URL string afterwards cannot fix
# that either. `task dev:local` and production both leave this unset, since S3_ENDPOINT_URL there is
# already reachable from wherever the browser runs.
S3_PUBLIC_ENDPOINT_URL = os.environ.get("S3_PUBLIC_ENDPOINT_URL", "") or S3_ENDPOINT_URL

# botocore >= 1.36 defaults to sending x-amz-checksum-crc32 with aws-chunked framing on every PUT,
# which S3-compatible providers — Hetzner AND MinIO — mis-store or reject. botocore reads these
# straight out of the environment itself, never through Django, so setdefault() here is what makes
# uploads work without an app/.env at all; .setdefault leaves an explicit .env/real env var alone.
os.environ.setdefault("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
os.environ.setdefault("AWS_RESPONSE_CHECKSUM_VALIDATION", "when_required")

_MEDIA_S3_OPTIONS = {
    "bucket_name": S3_BUCKET,
    # minioadmin/minioadmin (MinIO's own defaults) when nothing is configured — see S3_BUCKET above.
    "access_key": os.environ.get("S3_ACCESS_KEY", "minioadmin" if DEBUG else ""),
    "secret_key": os.environ.get("S3_SECRET_KEY", "minioadmin" if DEBUG else ""),
    "endpoint_url": S3_ENDPOINT_URL,
    "region_name": S3_LOCATION,
    # Virtual-host style is what Hetzner documents: https://<bucket>.<loc>.your-objectstorage.com.
    # The bucket name must therefore be DNS-safe — lowercase, and NO DOTS, or TLS SNI against their
    # wildcard certificate fails for every request. MinIO (local dev) overrides this to "path" via
    # S3_ADDRESSING_STYLE, since it has no such certificate.
    "addressing_style": S3_ADDRESSING_STYLE,
    "signature_version": "s3v4",
    # None, not "private". Hetzner implements bucket policies and not S3 ACLs, and rejects the
    # x-amz-acl header outright.
    "default_acl": None,
    # The bucket is private; core.media.serve_media issues a presigned GET per request.
    "querystring_auth": True,
    # Kept in step with core.media.PRESIGN_TTL, which is what actually signs the URLs we serve.
    "querystring_expire": 3600,
    # FALSE, and django-storages defaults it to True — which skips Django's name-suffixing entirely.
    # Resident.profile_picture uploads to a flat "profile_pictures/" with no date and no uniquifier,
    # so with overwriting on, the second resident to upload an IMG_1234.jpg silently replaces the
    # first one's photo and the first one's row then points at somebody else's face.
    "file_overwrite": False,
    # Shares the bucket with the database backups under "backups/", so the prefix is a security
    # boundary, not tidiness — see core.storage.MEDIA_PREFIX. MediaS3Storage defaults it; named here
    # so the grep for "backups" finds this comment.
    "location": "media",
    "object_parameters": {"CacheControl": "private, max-age=604800"},
}

# Photo-album originals/derivatives (photo_album.storage.PhotoAlbumS3Storage): same bucket and
# credentials, but its OWN top-level key — "photo-album/…", never "media/photo-album/…". Unlike the
# options above, nothing here is served through Django: .url() returns a presigned bucket URL
# straight from S3, so "location" stays empty rather than "media" — see photo_album/storage.py.
PHOTO_ALBUM_S3_OPTIONS = {**_MEDIA_S3_OPTIONS, "location": ""}

# WhiteNoise hashed/compressed static in prod; plain storage in dev so {% static %} needs no manifest.
STORAGES = {
    "default": {"BACKEND": "core.storage.MediaS3Storage", "OPTIONS": _MEDIA_S3_OPTIONS},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# Secrets (WiFi/calendar/SMTP) come from the environment (.env / vault), never source. (F-013)
LOGIN_URL = "/intern/admin/login"
LOGIN_REDIRECT_URL = "/intern/"
LOGOUT_REDIRECT_URL = "/intern/admin/login"

# Email — defaults to the console backend in dev (prints instead of sending). SMTP from env in prod.
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.environ.get("SMTP_HOST", "")
EMAIL_HOST_USER = os.environ.get("SMTP_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
EMAIL_PORT = int(os.environ.get("SMTP_PORT", "587"))
# Port 465 = implicit TLS/SMTPS (e.g. one.com's send.one.com); 587 = STARTTLS. Django forbids both.
EMAIL_USE_SSL = EMAIL_PORT == 465
EMAIL_USE_TLS = not EMAIL_USE_SSL
# Sender addresses. one.com rejects any From the SMTP_USER account is not itself or an alias of
# ("550 5.7.1 [M9] User [x] not authorized to send on behalf of <y>"), so every *_FROM_EMAIL below
# must be aliased onto SMTP_USER in the one.com control panel — otherwise nothing is delivered.
# Verify each one after changing SMTP_USER:  manage.py sendtestemail you@example.com
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "autosvar@gahk.dk")
# Sender for the ølkælder debt-warning mails (legacy used bierkeller@gahk.dk).
OELKAELDER_FROM_EMAIL = os.environ.get("OELKAELDER_FROM_EMAIL", "bierkeller@gahk.dk")
# Recipient (not a sender) — where the admissions committee notifications go.
INDSTILLING_EMAIL = os.environ.get("INDSTILLING_EMAIL", "indstillingen@gahk.dk")
# Ølkælder bank account shown on the member's saldo page (where to transfer money to top up).
OELKAELDER_BANK_REG = os.environ.get("OELKAELDER_BANK_REG", "9070")
OELKAELDER_BANK_ACCOUNT = os.environ.get("OELKAELDER_BANK_ACCOUNT", "1642635456")

# Front-page visit counter: server-side secret for HMAC-hashing visitor IPs (F-002/F-011).
VISIT_COUNTER_HMAC_KEY = os.environ.get("VISIT_COUNTER_HMAC_KEY", "dev-hmac-key")

# Cloudflare Turnstile on the public application forms (F-001). Unset in dev → the check is skipped.
TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "")

# Ølkælder till is an open kiosk on the GAHK LAN (F-003): purchases allowed without per-user login,
# but only from these source IPs (as seen by the server). In DEBUG the gate is open for testing.
OELKAELDER_KIOSK_IPS = [ip for ip in os.environ.get("OELKAELDER_KIOSK_IPS", "").split(",") if ip]

# GAHK Wiki — standalone MediaWiki, served at /wiki/ in prod (legacy path). Point WIKI_URL at the
# preview container (e.g. http://localhost:8899) during local development.
WIKI_URL = os.environ.get("WIKI_URL", "/wiki/")

# Where residents report bugs / request features. Defaults to the project's GitHub issue chooser;
# override FEEDBACK_URL if the repo moves or a different tracker is used.
FEEDBACK_URL = os.environ.get("FEEDBACK_URL", "https://github.com/GAHK-org/gahk_intern/issues/new/choose")

# Room-inspection photo uploads (F-005): server-side hard cap. Images are also downscaled client-side
# before upload, so this is mainly a backstop against oversized/crafted uploads.
ROOM_PHOTO_MAX_MB = int(os.environ.get("ROOM_PHOTO_MAX_MB", "5"))

# Web Push for Den Hurtige (the PWA that replaces the Messenger group). The two keys are the RAW
# base64url VAPID pair, NOT the .pem files: VAPID_PUBLIC_KEY is handed to the browser as
# `applicationServerKey`, which must be the 65-byte uncompressed EC point. app/.env.example shows how
# to derive both from an existing PEM. Unset in dev → the subscribe button reports push unavailable.
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
# Must be a real, monitored address: push services contact it about delivery problems, and some
# reject pushes whose VAPID `sub` claim is not a usable mailto.
VAPID_ADMIN_EMAIL = os.environ.get("VAPID_ADMIN_EMAIL", "autosvar@gahk.dk")

# Optional image on a Den Hurtige post: server-side hard cap, same backstop as the room photos.
QUICK_POST_MAX_MB = int(os.environ.get("QUICK_POST_MAX_MB", "5"))

# Django's default logging config only wires up its own `django.*` loggers; anything our code logs
# reaches stderr only at WARNING+, via logging's last-resort handler. Den Hurtige delivers push on a
# background thread, where "nothing happened" and "every send failed" look identical without a log
# line — so its logger gets an explicit console handler.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "[{levelname}] {name}: {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "loggers": {
        "den_hurtige": {
            "handlers": ["console"],
            "level": os.environ.get("DEN_HURTIGE_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        # core.push logs the "delivered to n/m device(s)" line for every fan-out. Without an explicit
        # handler it would only surface at WARNING+, and that line is the only thing that tells "no
        # subscribers" apart from "every send failed" — which look identical from the outside and
        # both look exactly like push being broken.
        "core": {
            "handlers": ["console"],
            "level": os.environ.get("CORE_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
    },
}

# CMS image uploads (editors add pictures from the admin instead of committing them to the repo).
CMS_IMAGE_MAX_MB = int(os.environ.get("CMS_IMAGE_MAX_MB", "5"))

# Opslagstavlen image uploads (inserted into a post's Markdown from the compose toolbar). Its own
# setting rather than sharing the CMS one: every feature here caps its own uploads, and an ops
# change for the CMS must not silently change what residents may post.
NOTICE_IMAGE_MAX_MB = int(os.environ.get("NOTICE_IMAGE_MAX_MB", "5"))

# One hero image per event. Its own setting for the same reason the two above are separate: each
# feature caps its own uploads, so an ops change for one cannot silently change what residents may
# post to another.
EVENT_IMAGE_MAX_MB = int(os.environ.get("EVENT_IMAGE_MAX_MB", "5"))

# The ceiling for an ANIMATED image, which cuts across the per-feature caps above rather than
# joining them, because what it answers is a question about the FORMAT.
#
# Every cap above is written for a file that has already been through the browser downscaler, where
# a phone photograph becomes a few hundred KB. An animation never goes through it: the canvas step
# cannot compress an animation, only flatten it to its first frame, so frontend/src/imageupload.ts
# passes one through at whatever size the resident picked (see `isAnimatedUpload` there). Measured
# against 5 MB — a ceiling that assumes the downscaler ran — an ordinary reaction GIF from Giphy or
# Tenor is refused, which is precisely the thing Den Hurtige, opslagstavlen and begivenheder wanted
# to allow.
#
# One knob for every feature, unlike the caps above, and deliberately: the reason for the higher
# number is the format, so splitting it per feature would be four settings that must all hold the
# same value to avoid a GIF being postable in one place and not the next.
ANIMATED_IMAGE_MAX_MB = int(os.environ.get("ANIMATED_IMAGE_MAX_MB", "5"))

# Photo album uploads. Two ceilings rather than one: the album deliberately keeps the ORIGINAL at
# full resolution (unlike every other feature here, which downscales in the browser first), so a
# modern phone photo legitimately arrives at 10-15 MB, and a clip from the same phone is an order of
# magnitude larger again. Its own settings for the same reason the *_MAX_MB above are separate —
# each feature caps its own uploads, so an ops change for one cannot silently change another.
PHOTO_ALBUM_IMAGE_MAX_MB = int(os.environ.get("PHOTO_ALBUM_IMAGE_MAX_MB", "50"))
PHOTO_ALBUM_VIDEO_MAX_MB = int(os.environ.get("PHOTO_ALBUM_VIDEO_MAX_MB", "1000"))
