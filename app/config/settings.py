"""Django settings for the GAHK rewrite (config project).

Schema/decisions: see ../02-schema-etl.md. Target DB is PostgreSQL (via DATABASE_URL);
falls back to SQLite for local dev/validation when DATABASE_URL is unset.
"""

import os
from pathlib import Path

import dj_database_url
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

DATABASES = {
    "default": dj_database_url.config(
        default=os.environ.get("DATABASE_URL", f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
        conn_max_age=600,
    )
}

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

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]  # holds the Vite-built bundle (static/dist/)

# Media (user uploads) go to Hetzner Object Storage when S3_BUCKET is set, and to MEDIA_ROOT on the
# local disk when it is not — which is dev, CI, and prod before the migration. The env variable IS
# the switch: there is no separate flag to forget, and an unset bucket cannot half-enable anything.
#
# MEDIA_URL stays "/media/" either way. That is not an oversight and it is not optional: it is a
# prefix of content stored in the database, and core.checks (core.E007-E009) refuses to start the
# process if it or the backend's URLs ever stop matching. core/storage.py has the full argument.
S3_BUCKET = os.environ.get("S3_BUCKET", "")
# Acknowledges local-disk media in a DEBUG-off environment. There is exactly one honest reason to
# set it — a prod-shaped box with no bucket of its own (staging), or rehearsing the rollback while
# MEDIA_ROOT still has files. core.E010 refuses to start without it, because since the prod media
# volume was emptied an unset S3_BUCKET serves nothing and writes uploads to a disk nobody backs up.
ALLOW_LOCAL_MEDIA = os.environ.get("ALLOW_LOCAL_MEDIA", "") == "1"
# fsn1 (Falkenstein) / nbg1 (Nuremberg) / hel1 (Helsinki). Keep this in the same location as the VM:
# traffic inside eu-central does not count against the account's egress allowance.
S3_LOCATION = os.environ.get("S3_LOCATION", "fsn1")

_MEDIA_S3_OPTIONS = {
    "bucket_name": S3_BUCKET,
    "access_key": os.environ.get("S3_ACCESS_KEY", ""),
    "secret_key": os.environ.get("S3_SECRET_KEY", ""),
    "endpoint_url": f"https://{S3_LOCATION}.your-objectstorage.com",
    "region_name": S3_LOCATION,
    # Virtual-host style is what Hetzner documents: https://<bucket>.<loc>.your-objectstorage.com.
    # The bucket name must therefore be DNS-safe — lowercase, and NO DOTS, or TLS SNI against their
    # wildcard certificate fails for every request.
    "addressing_style": "virtual",
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

# WhiteNoise hashed/compressed static in prod; plain storage in dev so {% static %} needs no manifest.
STORAGES = {
    "default": (
        {"BACKEND": "core.storage.MediaS3Storage", "OPTIONS": _MEDIA_S3_OPTIONS}
        if S3_BUCKET
        else {"BACKEND": "django.core.files.storage.FileSystemStorage"}
    ),
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
