"""Test settings: identical to production settings but with a fast password hasher.

PBKDF2 is intentionally slow (that is its security property), but during tests every
make_resident() call hashes a password. Swapping to MD5 removes that bottleneck without
affecting any test behaviour — tests that care about hashing (e.g. the legacy-upgrade test)
should assert the *outcome* (the hash changed), not a specific target algorithm.
"""

from config.settings import *  # noqa: F403

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
    "core.hashers.GahkLegacySHA256PasswordHasher",
]

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# A registered model written by a request or a job with no audit event open is a missing
# trail.event() — an error here, a logged warning in production. Writes from the tests' own setup
# (outside any request or job) are not recorded at all.
AUDIT_STRICT = True
AUDIT_FALLBACK = False
