"""Legacy password hasher.

The old site stored passwords as a *salt-less* SHA-256 hex digest in `intern_alumne.password`; the
migration imported them as ``gahk_sha256$$<hexdigest>``. This hasher lets
Django verify those on login; because ``must_update`` returns True, Django immediately re-hashes the
password with the default (strong) hasher on the next successful login — the upgrade-on-login path.
No forced global reset. Residents who have not logged in since the migration still hold such a
hash, so this must stay until none are left (the wiki's GahkAuthLogin checks the same format).
"""

import hashlib
from typing import TYPE_CHECKING, Any, Literal

from django.contrib.auth.hashers import BasePasswordHasher
from django.utils.crypto import constant_time_compare

if TYPE_CHECKING:
    from django.utils.functional import _StrPromise


class GahkLegacySHA256PasswordHasher(BasePasswordHasher):
    algorithm = "gahk_sha256"

    def salt(self) -> Literal[""]:
        return ""  # legacy hashes were unsalted

    def encode(self, password: str, salt: str = "") -> str:
        digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return f"{self.algorithm}$${digest}"

    def verify(self, password: str, encoded: str) -> bool:
        return constant_time_compare(self.encode(password), encoded)

    def safe_summary(self, encoded: str) -> "dict[str | _StrPromise, Any]":
        algorithm, _, digest = encoded.partition("$$")
        return {"algorithm": algorithm, "hash": digest[:6] + "…"}

    def must_update(self, encoded: str) -> bool:
        return True  # always upgrade legacy hashes to the default hasher on next login

    def harden_runtime(self, password: str, encoded: str) -> None:
        return
