import hashlib
from collections.abc import Callable

import pytest

from residents.models import Resident, RoleAssignment, active_period


@pytest.fixture(autouse=True)
def _isolate_from_dotenv(settings: object) -> None:
    """Neutralise the developer's app/.env for the whole suite.

    config/settings.py calls `load_dotenv(BASE_DIR / ".env")`, so anything a developer puts there to
    exercise a real integration also reaches `task test`. That has already caused two different
    kinds of confusion, and neither announced itself as an environment problem:

      * S3_BUCKET — several tests upload files and several assert that deleting a row deletes the
        file, so the suite would write to and then delete objects in the PRODUCTION bucket.
      * TURNSTILE_SECRET_KEY — with a secret set, admissions._verify_turnstile stops short-circuiting
        and looks for a `cf-turnstile-response` token that no test posts, so every application form
        is silently rejected. That surfaces as `assert 0 == 1` on an Application count: it reads like
        a broken view, and three tests failed this way for some time while CI stayed green.

    Autouse and unconditional, because in both cases the symptom points somewhere other than the
    cause, and no individual test should have to remember. Anything else added to .env that changes
    behaviour rather than merely configuring an address belongs here too.
    """
    settings.STORAGES = {  # type: ignore[attr-defined]
        **settings.STORAGES,  # type: ignore[attr-defined]
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    }
    settings.TURNSTILE_SECRET_KEY = ""  # type: ignore[attr-defined]


@pytest.fixture
def make_resident(db: None) -> Callable[[str, str, bool, tuple], Resident]:
    def _make(
        email: str = "r@gahk.dk",
        password: str = "hemmelig",
        legacy: bool = False,
        roles: tuple = (),
        **extra: object,
    ) -> Resident:
        r = Resident(
            email=email,
            first_name=extra.pop("first_name", "Test"),
            last_name=extra.pop("last_name", "Beboer"),
            **extra,
        )
        if legacy:
            r.password = "gahk_sha256$$" + hashlib.sha256(password.encode()).hexdigest()
        else:
            r.set_password(password)
        r.save()
        year, month = active_period()
        for role in roles:
            RoleAssignment.objects.create(resident=r, role=role, year=year, month=month)
        if roles:
            Resident.objects.filter(id=r.id).update(is_staff=True)
        return r

    return _make
