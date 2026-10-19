"""Role-preview / "view site as role" tests.

Locks in the security boundary: only a real superuser/administrator may preview; the preview restricts
(never escalates) access; a forged session key on a non-admin is ignored; the nav reflects the effective
role; and logout clears the override.

Route reference: /admin/preview/set (siteadmin:preview_set), /intern/ak/admin (ak overview, role ak),
/optagelse/listansoegninger (admissions list, role indstilling).
"""

from collections.abc import Callable

import pytest
from django.test import Client

from residents.models import Resident, Role


def _client(user: Resident) -> Client:
    c = Client()
    c.force_login(user=user)
    return c


@pytest.mark.django_db
def test_admin_preview_as_ak_restricts(make_resident: Callable) -> None:
    admin = make_resident(email="admin@gahk.dk", roles=[Role.ADMINISTRATOR])
    c = _client(admin)
    assert c.get("/optagelse/listansoegninger").status_code == 200  # baseline: all-access
    c.post("/admin/preview/set", {"mode": "role", "role": "ak"})
    assert c.get("/intern/ak/admin").status_code == 200  # ak view now visible
    assert c.get("/optagelse/listansoegninger").status_code == 403  # indstilling lost


@pytest.mark.django_db
def test_admin_preview_beboer_loses_officer_access(make_resident: Callable) -> None:
    admin = make_resident(email="admin@gahk.dk", roles=[Role.ADMINISTRATOR])
    c = _client(admin)
    c.post("/admin/preview/set", {"mode": "resident"})
    assert c.get("/intern/ak/admin").status_code == 403
    r = c.get("/intern/")
    assert r.status_code == 200
    assert "Forhåndsvisning" in r.content.decode()  # banner shown


@pytest.mark.django_db
def test_forged_preview_key_ignored_for_non_admin(make_resident: Callable) -> None:
    ak = make_resident(email="ak@gahk.dk", roles=[Role.AK])
    c = _client(ak)
    s = c.session
    s["preview_roles"] = ["administrator"]  # forge an all-access override
    s.save()
    assert c.get("/optagelse/listansoegninger").status_code == 403  # not escalated
    assert c.get("/intern/ak/admin").status_code == 200  # real ak access intact


@pytest.mark.django_db
def test_non_admin_cannot_use_switcher(make_resident: Callable) -> None:
    ak = make_resident(email="ak@gahk.dk", roles=[Role.AK])
    c = _client(ak)
    assert c.post("/admin/preview/set", {"mode": "admin"}).status_code == 403
    assert c.get("/admin/preview").status_code == 403


@pytest.mark.django_db
def test_nav_changes_under_preview(make_resident: Callable) -> None:
    admin = make_resident(email="admin@gahk.dk", roles=[Role.ADMINISTRATOR])
    c = _client(admin)
    c.post("/admin/preview/set", {"mode": "role", "role": "ak"})
    html = c.get("/intern/").content.decode()
    assert "AK-oversigt" in html
    assert "Ansøgninger" not in html and "Site-admin" not in html
    c.post("/admin/preview/set", {"mode": "role", "role": "indstilling"})
    html = c.get("/intern/").content.decode()
    assert "Ansøgninger" in html
    assert "AK-oversigt" not in html


@pytest.mark.django_db
def test_clearing_preview_restores_admin(make_resident: Callable) -> None:
    admin = make_resident(email="admin@gahk.dk", roles=[Role.ADMINISTRATOR])
    c = _client(admin)
    c.post("/admin/preview/set", {"mode": "resident"})
    assert c.get("/optagelse/listansoegninger").status_code == 403
    c.post("/admin/preview/set", {"mode": "clear"})
    assert c.get("/optagelse/listansoegninger").status_code == 200
    assert "Forhåndsvisning" not in c.get("/intern/").content.decode()


@pytest.mark.django_db
def test_logout_clears_preview(make_resident: Callable) -> None:
    admin = make_resident(email="admin@gahk.dk", roles=[Role.ADMINISTRATOR])
    c = _client(admin)
    c.post("/admin/preview/set", {"mode": "role", "role": "ak"})
    c.post("/intern/admin/logout")
    assert "preview_roles" not in c.session
    c.force_login(admin)
    assert c.get("/optagelse/listansoegninger").status_code == 200


@pytest.mark.django_db
def test_superuser_preview_restricts(make_resident: Callable) -> None:
    su = make_resident(email="su@gahk.dk", is_superuser=True)
    c = _client(su)
    assert c.get("/optagelse/listansoegninger").status_code == 200  # superuser = all-access
    c.post("/admin/preview/set", {"mode": "role", "role": "ak"})
    assert c.get("/optagelse/listansoegninger").status_code == 403  # restricted under preview
    c.post("/admin/preview/set", {"mode": "clear"})
    assert c.get("/optagelse/listansoegninger").status_code == 200


@pytest.mark.django_db
def test_plain_resident_and_anon_have_no_admin_nav(make_resident: Callable) -> None:
    beboer = make_resident(email="b@gahk.dk")
    html = _client(beboer).get("/intern/").content.decode()
    # Værelsestjek is deliberately absent from this list: it is a base item for every resident (F-005).
    for label in ("AK-oversigt", "Ansøgninger", "Site-admin", "Ølkælder-admin", "Regnskab"):
        assert label not in html
    assert "Alumneliste" not in Client().get("/").content.decode()  # anon front page: no intern nav
