"""Regression tests codifying the security-critical behaviour verified during the build.

These lock in the fixes from the Phase-3 threat model so they can't regress: legacy-hash upgrade,
monthly roles, admissions email/CSRF rules, POS money integrity, and the front-page visit counter.
"""

from collections.abc import Callable

import pytest
from django.contrib.auth.hashers import identify_hasher
from django.core import mail
from django.test import Client
from django.utils import timezone

from admissions.models import Application
from residents.models import Resident, Role, active_period


# ---------------------------------------------------------------- auth (F-014)
@pytest.mark.django_db
def test_legacy_sha256_upgrades_on_login(make_resident: Callable) -> None:
    make_resident(email="a@gahk.dk", password="hemmelig", legacy=True)
    c = Client()
    assert c.login(email="a@gahk.dk", password="hemmelig") is True
    r = Resident.objects.get(email="a@gahk.dk")
    assert identify_hasher(r.password).algorithm != "gahk_sha256"  # upgraded away from the legacy hash


@pytest.mark.django_db
def test_email_login_is_case_insensitive(make_resident: Callable) -> None:
    """Email is the login username; it must not be case-sensitive (auth/login ticket) — a capital
    first letter used to lock residents out."""
    make_resident(email="mixed@gahk.dk", password="hemmelig")
    c = Client()
    assert c.login(email="Mixed@gahk.dk", password="hemmelig") is True  # capital first letter
    c.logout()
    assert c.login(email="MIXED@GAHK.DK", password="hemmelig") is True  # all caps
    c.logout()
    assert c.login(email="mixed@gahk.dk", password="hemmelig") is True  # exact still works
    assert c.login(email="mixed@gahk.dk", password="forkert") is False  # password still enforced


@pytest.mark.django_db
def test_password_reset_is_case_insensitive(make_resident: Callable) -> None:
    """Recover-password looks the account up case-insensitively too (Django's PasswordResetForm uses
    email__iexact) — a capital first letter must still receive the reset link."""
    make_resident(email="reset@gahk.dk", password="hemmelig")  # usable password → eligible for reset
    mail.outbox = []
    resp = Client().post("/intern/admin/password-reset", {"email": "Reset@gahk.dk"})
    assert resp.status_code == 302  # redirects to the "done" page
    assert len(mail.outbox) == 1  # reset link sent despite the capital
    assert "reset@gahk.dk" in mail.outbox[0].to


@pytest.mark.django_db
def test_reset_reaches_a_resident_who_never_set_a_password() -> None:
    """A newly created resident has no usable password until they follow the welcome link, which is a
    reset token and expires after 2h. Django's stock PasswordResetForm skips such accounts, so the
    "glemt kodeord" fallback the welcome mail points at sent nothing at all — and the view redirects
    to "done" regardless, so the lockout was silent. ResidentPasswordResetForm must reach them."""
    r = Resident(email="ny@gahk.dk", first_name="Ny", last_name="Beboer")
    r.set_unusable_password()  # exactly what the alumneliste's add_new does
    r.save()
    mail.outbox = []
    resp = Client().post("/intern/admin/password-reset", {"email": "ny@gahk.dk"})
    assert resp.status_code == 302
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["ny@gahk.dk"]


@pytest.mark.django_db
def test_reset_still_ignores_deactivated_residents() -> None:
    """Widening the form to unusable passwords must not also let a moved-out resident back in:
    is_active stays the gate."""
    r = Resident(email="ude@gahk.dk", first_name="Ude", last_name="Flyttet", is_active=False)
    r.set_unusable_password()
    r.save()
    mail.outbox = []
    assert Client().post("/intern/admin/password-reset", {"email": "ude@gahk.dk"}).status_code == 302
    assert mail.outbox == []  # no link for a deactivated account


@pytest.mark.django_db
def test_monthly_role_is_time_bound(make_resident: Callable) -> None:
    r = make_resident(roles=[Role.AK])
    y, m = active_period()
    assert r.has_role(Role.AK, (y, m)) is True
    assert r.has_role(Role.AK, (y - 1, m)) is False
    assert r.has_role(Role.INDSTILLING, (y, m)) is False


# ---------------------------------------------------------------- admissions (F-001)
def _rundvisning_data(motivation: str) -> dict[str, str]:
    return {
        "full_name": "Ny",
        "email": "ny@x.dk",
        "gender": "male",
        "age": "22",
        "study_year": "1",
        "year_left": "3",
        "university": "DTU",
        "field_of_study": "Fysik",
        "heard_about_us": "plakat",
        "motivation": motivation,
    }


def test_rundvisning_motivation_renders_500_character_limit() -> None:
    response = Client().get("/optagelse/ansoeg")

    assert response.status_code == 200
    assert response.context["form"]["motivation"].field.max_length == 500
    assert 'maxlength="500"' in str(response.context["form"]["motivation"])


@pytest.mark.django_db
def test_rundvisning_accepts_500_character_motivation() -> None:
    motivation = ("a " * 249) + "ab"
    assert len(motivation) == 500

    response = Client().post("/optagelse/send_rundvisning", _rundvisning_data(motivation))

    assert response.status_code == 302
    assert Application.objects.get().motivation == motivation


@pytest.mark.django_db
def test_rundvisning_rejects_501_character_motivation() -> None:
    motivation = ("a " * 249) + "abc"
    assert len(motivation) == 501

    response = Client().post("/optagelse/send_rundvisning", _rundvisning_data(motivation))

    assert response.status_code == 200
    assert "motivation" in response.context["form"].errors
    assert not Application.objects.exists()


@pytest.mark.django_db
def test_rundvisning_emails_applicant_only() -> None:
    """The committee is no longer notified per rundvisning request; only the applicant auto-reply
    goes out (F-011)."""
    mail.outbox = []
    Client().post(
        "/optagelse/send_rundvisning",
        {
            "full_name": "Ny",
            "email": "ny@x.dk",
            "gender": "male",
            "age": "22",
            "study_year": "1",
            "year_left": "3",
            "university": "DTU",
            "field_of_study": "Fysik",
            "heard_about_us": "plakat",
            "motivation": "m",
        },
    )
    assert Application.objects.filter(type="rundvisning").count() == 1
    assert len(mail.outbox) == 1  # applicant auto-reply only, committee not emailed
    assert mail.outbox[0].to == ["ny@x.dk"]


@pytest.mark.django_db
def test_fremleje_does_not_email_committee() -> None:
    mail.outbox = []
    Client().post(
        "/optagelse/send_fremleje",
        {
            "full_name": "Ny",
            "email": "ny@x.dk",
            "gender": "other",
            "age": "25",
            "occupation": "Studerende",
            "heard_about_us": "avis",
            "motivation": "m",
        },
    )
    assert Application.objects.filter(type="fremleje").count() == 1
    assert len(mail.outbox) == 1  # applicant auto-reply only (decision 2026-06)


@pytest.mark.django_db
def test_mark_received_is_post_only_and_role_gated(make_resident: Callable) -> None:
    app = Application.objects.create(
        type="rundvisning", full_name="X", email="x@x.dk", submitted_at=timezone.now()
    )
    ind = make_resident(email="ind@gahk.dk", roles=[Role.INDSTILLING])
    c = Client()
    assert c.get("/optagelse/listansoegninger").status_code in (302, 301)  # anon → login
    c.force_login(ind)
    assert c.get(f"/optagelse/setasreceived/{app.id}").status_code == 405  # GET blocked
    assert c.post(f"/optagelse/setasreceived/{app.id}").status_code == 302
    app.refresh_from_db()
    assert app.received_by_id == ind.id


@pytest.mark.django_db
def test_applications_search_spans_all_pages(make_resident: Callable) -> None:
    """Search filters the whole queryset before pagination, so a match on page 2 is found (F-011)."""
    # Created first so that under the default newest-first order it's buried past page 1 (50/page).
    Application.objects.create(
        type="rundvisning",
        full_name="Zenobia Needle",
        email="z@x.dk",
        university="Roskilde Universitet",
        submitted_at=timezone.now(),
    )
    for i in range(55):  # > one page (50)
        Application.objects.create(
            type="rundvisning", full_name=f"Filler {i}", email=f"f{i}@x.dk", submitted_at=timezone.now()
        )
    c = Client()
    c.force_login(make_resident(email="ind-s@gahk.dk", roles=[Role.INDSTILLING]))

    # Without search the unique applicant is buried past page 1.
    page1 = c.get("/optagelse/listansoegninger").content.decode()
    assert "Zenobia Needle" not in page1
    # Searching by name finds it regardless of page.
    hit = c.get("/optagelse/listansoegninger", {"q": "Zenobia"}).content.decode()
    assert "Zenobia Needle" in hit and "Filler" not in hit
    # Search also matches uddannelse (a non-name column).
    by_uni = c.get("/optagelse/listansoegninger", {"q": "Roskilde"}).content.decode()
    assert "Zenobia Needle" in by_uni


@pytest.mark.django_db
def test_applications_sortable_by_uddannelse(make_resident: Callable) -> None:
    """Columns can be sorted; e.g. by uddannelse asc/desc (F-011). Junk sort falls back safely."""
    Application.objects.create(
        type="rundvisning",
        full_name="Aaron",
        email="a@x.dk",
        university="Zoologi",
        submitted_at=timezone.now(),
    )
    Application.objects.create(
        type="rundvisning",
        full_name="Bea",
        email="b@x.dk",
        university="Antropologi",
        submitted_at=timezone.now(),
    )
    c = Client()
    c.force_login(make_resident(email="ind-o@gahk.dk", roles=[Role.INDSTILLING]))

    asc = c.get("/optagelse/listansoegninger", {"sort": "uddannelse", "dir": "asc"}).content.decode()
    assert asc.index("Bea") < asc.index("Aaron")  # Antropologi before Zoologi
    desc = c.get("/optagelse/listansoegninger", {"sort": "uddannelse", "dir": "desc"}).content.decode()
    assert desc.index("Aaron") < desc.index("Bea")
    # Junk sort/dir must not crash and falls back to the default (newest first, both present).
    bad = c.get("/optagelse/listansoegninger", {"sort": "x", "dir": "y"})
    assert bad.status_code == 200 and "Aaron" in bad.content.decode()
    # No unrendered template syntax leaks onto the page.
    assert "{%" not in asc and "{#" not in asc


@pytest.mark.django_db
def test_discarded_application_hidden_from_list_and_search_with_toggle(make_resident: Callable) -> None:
    """Indstillingen can discard an application; it then drops out of the list and search unless the
    show-discarded toggle is on, and can be un-discarded (F-011)."""
    app = Application.objects.create(
        type="rundvisning",
        full_name="Spam Bot",
        email="s@x.dk",
        university="Nowhere",
        submitted_at=timezone.now(),
    )
    ind = make_resident(email="ind-d@gahk.dk", roles=[Role.INDSTILLING])
    c = Client()
    c.force_login(ind)

    # GET on the discard endpoint is blocked; POST toggles it on.
    assert c.get(f"/optagelse/kasser/{app.id}").status_code == 405
    assert c.post(f"/optagelse/kasser/{app.id}").status_code == 302
    app.refresh_from_db()
    assert app.discarded_by_id == ind.id and app.discarded_at is not None

    # Hidden from the default list and from search.
    default = c.get("/optagelse/listansoegninger").content.decode()
    assert "Spam Bot" not in default
    assert "Spam Bot" not in c.get("/optagelse/listansoegninger", {"q": "Spam"}).content.decode()

    # Visible (and flagged) when the toggle is on.
    shown = c.get("/optagelse/listansoegninger", {"show_discarded": "1"}).content.decode()
    assert "Spam Bot" in shown and "Kasseret" in shown
    assert (
        "Spam Bot"
        in c.get("/optagelse/listansoegninger", {"q": "Spam", "show_discarded": "1"}).content.decode()
    )

    # Toggling again un-discards it, and it returns to the normal list.
    assert c.post(f"/optagelse/kasser/{app.id}").status_code == 302
    app.refresh_from_db()
    assert app.discarded_by_id is None and app.discarded_at is None
    assert "Spam Bot" in c.get("/optagelse/listansoegninger").content.decode()


@pytest.mark.django_db
def test_applications_pending_filter(make_resident: Callable) -> None:
    """The 'kun afventende' filter shows only not-yet-received applications, and composes with search
    and the discarded filter (F-011)."""
    ind = make_resident(email="ind-p@gahk.dk", roles=[Role.INDSTILLING])
    waiting = Application.objects.create(
        type="rundvisning", full_name="Waiting Wanda", email="w@x.dk", submitted_at=timezone.now()
    )
    done = Application.objects.create(
        type="rundvisning",
        full_name="Done Dora",
        email="d@x.dk",
        submitted_at=timezone.now(),
        received_by=ind,
        received_at=timezone.now(),
    )
    c = Client()
    c.force_login(ind)

    all_apps = c.get("/optagelse/listansoegninger").content.decode()
    assert "Waiting Wanda" in all_apps and "Done Dora" in all_apps
    pending = c.get("/optagelse/listansoegninger", {"pending": "1"}).content.decode()
    assert "Waiting Wanda" in pending and "Done Dora" not in pending
    assert waiting.received_by_id is None and done.received_by_id == ind.id


@pytest.mark.django_db
def test_applications_export_honours_filters_and_date_range(make_resident: Callable) -> None:
    """CSV/Excel export mirrors the list filters (search, kun afventende) plus a from/to date range,
    and emits the contact columns (F-011)."""
    ind = make_resident(email="ind-x@gahk.dk", roles=[Role.INDSTILLING])

    def app(**kw: object) -> Application:
        kw.setdefault("type", "rundvisning")
        kw.setdefault("submitted_at", timezone.now())
        return Application.objects.create(**kw)

    app(
        full_name="In Range",
        email="inrange@x.dk",
        university="DTU",
        submitted_at=timezone.now().replace(year=2026, month=6, day=15),
    )
    app(
        full_name="Too Early",
        email="early@x.dk",
        submitted_at=timezone.now().replace(year=2026, month=1, day=1),
    )
    app(
        full_name="Received Rita",
        email="rita@x.dk",
        received_by=ind,
        received_at=timezone.now(),
        submitted_at=timezone.now().replace(year=2026, month=6, day=20),
    )
    c = Client()
    c.force_login(ind)

    # CSV over June 2026 → only "In Range" (Too Early is outside; Received Rita drops with pending=1).
    csv_resp = c.get(
        "/optagelse/eksport",
        {"format": "csv", "from": "2026-06-01", "to": "2026-06-30", "pending": "1"},
    )
    assert csv_resp["Content-Type"].startswith("text/csv")
    body = csv_resp.content.decode("utf-8-sig")
    assert body.splitlines()[0] == "Dato,Type,Navn,E-mail,Uddannelse"  # contact columns only
    assert "inrange@x.dk" in body
    assert "early@x.dk" not in body and "rita@x.dk" not in body

    # Excel export returns a spreadsheet.
    xlsx = c.get("/optagelse/eksport", {"format": "xlsx"})
    assert xlsx["Content-Type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert xlsx.content[:2] == b"PK"  # xlsx is a zip

    # Role-gated.
    other = Client()
    other.force_login(make_resident(email="plain-x@gahk.dk"))
    assert other.get("/optagelse/eksport", {"format": "csv"}).status_code == 403


@pytest.mark.django_db
def test_discard_is_role_gated(make_resident: Callable) -> None:
    """A plain resident cannot discard an application."""
    app = Application.objects.create(
        type="rundvisning", full_name="X", email="x@x.dk", submitted_at=timezone.now()
    )
    c = Client()
    c.force_login(make_resident(email="plain@gahk.dk"))
    assert c.post(f"/optagelse/kasser/{app.id}").status_code == 403
    app.refresh_from_db()
    assert app.discarded_by_id is None


# ---------------------------------------------------------------- ølkælder money (F-003)
@pytest.mark.django_db
def test_purchase_split_is_exact_and_atomic(make_resident: Callable) -> None:
    from oelkaelder.models import Product, Shopper
    from oelkaelder.services import record_purchase

    p = Product.objects.create(name="Øl", price_ore=1500)
    s1 = Shopper.objects.create(resident=make_resident(email="s1@gahk.dk"))
    s2 = Shopper.objects.create(resident=make_resident(email="s2@gahk.dk"))
    txn = record_purchase([s1.id, s2.id], [{"product": p.id, "mode": "fixed", "qty": 3}])  # 4500 øre / 2
    total = sum(sh.share_ore for sh in txn.shares.all())
    assert total == 4500
    assert s1.balance_ore + s2.balance_ore == -4500  # derived from the ledger


@pytest.mark.django_db
def test_my_balance_shows_account_statement(make_resident: Callable) -> None:
    from oelkaelder.models import Product, Shopper
    from oelkaelder.services import record_deposit, record_purchase

    p = Product.objects.create(name="Øl", price_ore=1500)
    r = make_resident(email="buyer@gahk.dk")
    s = Shopper.objects.create(resident=r)
    record_deposit(s, 5000)  # 50,00 kr credit
    record_purchase(
        [s.id], [{"product": p.id, "mode": "fixed", "qty": 2}]
    )  # 30,00 kr debit, all to this shopper

    c = Client()
    c.force_login(r)
    html = c.get("/intern/oelkaelder/min-saldo").content.decode()
    assert "Kontoudtog" in html
    assert "2× Øl" in html and "-30,00 kr" in html  # purchase debit (signed)
    assert "Indbetaling" in html and "50,00 kr" in html  # deposit credit


@pytest.mark.django_db
def test_application_list_shows_receiver(make_resident: Callable) -> None:
    ind = make_resident(
        email="ind@gahk.dk", first_name="Ida", last_name="Storgaard", roles=[Role.INDSTILLING]
    )
    now = timezone.now()
    Application.objects.create(
        type=Application.Type.TOUR,
        full_name="Handled Person",
        email="h@x.dk",
        submitted_at=now,
        received_by=ind,
        received_at=now,
    )
    Application.objects.create(
        type=Application.Type.TOUR,
        full_name="Pending Person",
        email="p@x.dk",
        submitted_at=now,
    )
    c = Client()
    c.force_login(ind)
    html = c.get("/optagelse/listansoegninger").content.decode()
    assert "Ida S." in html  # receiver shown as first name + last initial
    assert "Afventer" in html  # the un-received one is flagged


@pytest.mark.django_db
def test_cms_admin_is_gated_to_content_editor_roles(make_resident: Callable) -> None:
    """Frontpage/CMS content is editable by administrator, indstilling, inspektion and pr — the
    content-editor roles — and by nobody else (other role-holders are is_staff but not editors)."""
    from cms.models import Page

    Page.objects.create(header="Testside", slug="testside", body="<p>hej</p>")

    for role in (Role.ADMINISTRATOR, Role.INDSTILLING, Role.INSPEKTION, Role.PR):
        c = Client()
        c.force_login(make_resident(email=f"{role.value}@gahk.dk", roles=[role]))
        for model in ("page", "newsitem", "event"):  # all CMS content, not just pages
            assert c.get(f"/django-admin/cms/{model}/").status_code == 200, f"{role} should edit {model}"

    for role in (Role.AK, Role.OELKAELDER, Role.KOKKENGRUPPE, Role.REGNSKAB):  # staff, but not editors
        c = Client()
        c.force_login(make_resident(email=f"{role.value}@gahk.dk", roles=[role]))
        assert c.get("/django-admin/cms/page/").status_code == 403, f"{role} must not edit CMS"


@pytest.mark.django_db
def test_pr_embedsgruppe_grants_cms_access(make_resident: Callable) -> None:
    """Being on the "PR-gruppen" embedsgruppe next month grants the pr role, hence CMS editing."""
    from core.models import Room, Workgroup
    from residents.models import Residency, active_period
    from residents.views import _sync_month_roles

    pr_group = Workgroup.objects.create(name="PR-gruppen")
    r = make_resident(email="prmember@gahk.dk")
    y, m = active_period()
    room = Room.objects.create(legacy_index=140, number=140, floor="stuen", side="mod gaden")
    Residency.objects.create(resident=r, room=room, workgroup=pr_group, year=y, month=m)
    _sync_month_roles(r.id, pr_group, y, m, is_admin=False)  # what the next-month editor runs

    assert r.has_role("pr", (y, m))
    c = Client()
    c.force_login(r)
    assert c.get("/django-admin/cms/page/").status_code == 200


@pytest.mark.django_db
def test_cms_admin_sanitizes_html_on_save(make_resident: Callable) -> None:
    from cms.models import Page

    page = Page.objects.create(header="S", slug="s", body="")
    admin = make_resident(email="admin@gahk.dk", roles=[Role.ADMINISTRATOR])
    c = Client()
    c.force_login(admin)
    c.post(
        f"/django-admin/cms/page/{page.id}/change/",
        {
            "slug": "s",
            "menu_category": "0",
            "header": "S",
            "background_image": "",
            "body": '<p>Hej</p><script>alert(1)</script><a href="javascript:evil()">x</a>',
        },
    )
    page.refresh_from_db()
    assert "<p>Hej</p>" in page.body  # safe markup kept
    assert "<script" not in page.body and "javascript:" not in page.body  # stripped


@pytest.mark.django_db
def test_is_staff_synced_with_roles(make_resident: Callable) -> None:
    from residents.models import RoleAssignment, active_period

    r = make_resident(email="plain@gahk.dk")  # no roles → not staff
    assert r.is_staff is False
    y, m = active_period()
    ra = RoleAssignment.objects.create(resident=r, role=Role.AK, year=y, month=m)
    r.refresh_from_db()
    assert r.is_staff is True  # gained a role → staff
    ra.delete()
    r.refresh_from_db()
    assert r.is_staff is False  # lost last role → no longer staff

    su = make_resident(email="su@gahk.dk", is_superuser=True, is_staff=True)
    RoleAssignment.objects.create(resident=su, role=Role.AK, year=y, month=m).delete()
    su.refresh_from_db()
    assert su.is_staff is True  # superuser stays staff regardless


@pytest.mark.django_db
def test_dashboard_shows_shared_calendar_credentials(
    make_resident: Callable, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_CALENDAR_USER", "gahkkalender@gmail.com")
    monkeypatch.setenv("GOOGLE_CALENDAR_PASSWORD", "dummy-pw")
    c = Client()
    c.force_login(make_resident(email="d@gahk.dk"))
    h = c.get("/intern/").content.decode()
    assert "gahkkalender@gmail.com" in h and "dummy-pw" in h  # both shown to logged-in residents
    # The embedded Google Calendar agenda (restored from the legacy dashboard) renders for logged-in users.
    assert "calendar.google.com/calendar/embed" in h
    assert "src=gahkkalender%40gmail.com" in h


@pytest.mark.django_db
def test_oelkaelder_kiosk_gate_uses_forwarded_ip() -> None:
    from django.test import override_settings

    with override_settings(DEBUG=False, OELKAELDER_KIOSK_IPS=["130.225.243.26"]):
        c = Client()
        # Traefik appends the real client IP as the last X-Forwarded-For hop.
        ok = c.get("/intern/oelkaelder/", HTTP_X_FORWARDED_FOR="130.225.243.26")
        assert ok.status_code == 200  # kiosk open from the dorm egress IP
        blocked = c.get("/intern/oelkaelder/", HTTP_X_FORWARDED_FOR="203.0.113.9")
        assert blocked.status_code == 403  # any other IP is denied


@pytest.mark.django_db
def test_media_files_are_served_in_prod() -> None:
    from pathlib import Path

    from django.conf import settings
    from django.test import override_settings

    # Under the cms/ prefix on purpose. This test is about ROUTING — WhiteNoise serves only static,
    # and DEBUG-only serving would 404 these in prod — so it uses the one prefix that is still
    # readable without a session (core.media.PUBLIC_PREFIXES), keeping the anonymous client, which
    # is the stronger statement about the route. The gate itself is covered in
    # tests/test_media_storage.py, including that every other prefix now requires a login.
    f = Path(settings.MEDIA_ROOT) / "cms" / "__test_serve.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("hello-media")
    try:
        with override_settings(DEBUG=False):  # prod-like: must still serve /media/
            r = Client().get("/media/cms/__test_serve.txt")
        assert r.status_code == 200
        assert b"".join(r.streaming_content) == b"hello-media"
    finally:
        f.unlink(missing_ok=True)


@pytest.mark.django_db
def test_room_photo_over_max_is_rejected(make_resident: Callable) -> None:
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.test import override_settings

    from core.models import Room
    from rooms.models import RoomConditionScore, RoomCriterion

    rm = Room.objects.create(legacy_index=90, number=90, floor="stuen", side="mod gaden")
    RoomCriterion.objects.create(code="floor", name="Gulv", options=5)
    ins = make_resident(email="ins@gahk.dk", roles=[Role.INSPEKTION])
    c = Client()
    c.force_login(ins)
    img = SimpleUploadedFile("p.jpg", b"x" * 2048, content_type="image/jpeg")
    with override_settings(ROOM_PHOTO_MAX_MB=0):  # cap 0 → any file counts as "too big"
        c.post(
            f"/intern/vaerelsestjek/besvar/{rm.number}",
            {"score_floor": "3", "comment_floor": "", "image_floor": img},
        )
    s = RoomConditionScore.objects.get(criterion__code="floor")
    assert s.score == 3  # the score/comment are still saved
    assert not s.photo  # the oversized photo was skipped


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("filename", "content_type"),
    [("evil.svg", "image/svg+xml"), ("evil.svg", "image/png")],  # declared, and disguised
)
def test_room_inspection_refuses_an_svg_photo(
    make_resident: Callable, filename: str, content_type: str
) -> None:
    """An SVG is a document, not a picture: served from our own /media/ origin a direct navigation
    executes its <script> as us. Værelsestjek is open to *every* resident, so before the validators
    were consolidated in core.uploads this was a same-origin script upload available to anyone —
    the old check only asked whether the content type began "image/".
    """
    from django.core.files.uploadedfile import SimpleUploadedFile

    from core.models import Room
    from rooms.models import RoomConditionScore, RoomCriterion

    rm = Room.objects.create(legacy_index=91, number=91, floor="stuen", side="mod gaden")
    RoomCriterion.objects.create(code="floor", name="Gulv", options=5)
    c = Client()
    c.force_login(make_resident(email="beboer-svg@gahk.dk"))
    evil = SimpleUploadedFile(filename, b"<svg xmlns='http://www.w3.org/2000/svg'/>", content_type)

    c.post(
        f"/intern/vaerelsestjek/besvar/{rm.number}",
        {"score_floor": "3", "comment_floor": "", "image_floor": evil},
    )

    s = RoomConditionScore.objects.get(criterion__code="floor")
    assert s.score == 3  # the rest of the inspection still saved
    assert not s.photo, "an SVG reached /media/"


def test_room_condition_image_urls() -> None:
    from rooms.models import RoomConditionScore

    # legacy `image` is a ';'-separated list with mixed public/ and /public/ prefixes
    s = RoomConditionScore(image="public/image/a.jpg;/public/image/b.jpg;")
    assert s.image_urls == ["/media/public/image/a.jpg", "/media/public/image/b.jpg"]
    assert RoomConditionScore(image="https://ex.com/y.jpg").image_urls == ["https://ex.com/y.jpg"]
    assert RoomConditionScore(image="").image_urls == []


def test_body_media_rewrites_relative_and_absolute_legacy_urls() -> None:
    from cms.templatetags.cms_extras import body_media

    html = (
        '<img src="/public/image/a.jpg">'
        '<img src="http://gahk.dk/public/image/b.jpg">'
        '<img src="https://www.gahk.dk/public/image/c.jpg">'
    )
    out = body_media(html)
    assert out.count('src="/static/legacy/image/') == 3  # all three rewritten
    assert "gahk.dk/public" not in out and 'src="/public' not in out  # no http/relative leftovers


# ---------------------------------------------------------------- visit counter (F-002/F-011)
@pytest.mark.django_db
def test_frontpage_counter_hashes_and_dedups() -> None:
    from stats.models import DailyVisitCount, VisitTally

    c = Client()
    c.get("/")
    c.get("/")  # same IP within the 30-min window → counted once
    assert VisitTally.objects.count() == 1
    assert VisitTally.objects.first().count == 1
    assert DailyVisitCount.objects.get(date=timezone.localdate()).count == 1
    assert len(VisitTally.objects.first().ip_hash) == 64  # HMAC-SHA256 hex, not a raw IP


@pytest.mark.django_db
def test_events_news_page_and_forside_teaser() -> None:
    from datetime import timedelta

    from cms.models import Event, NewsItem

    # localdate(), not date.today(): the views split upcoming from past on `timezone.localdate()`,
    # and a test that builds its data on the system clock instead is asking a different question
    # than the code answers. They agree in Europe/Copenhagen, so this has never been a live bug —
    # but it is the difference between a fixture that tracks the code's clock and one that drifts.
    today = timezone.localdate()
    Event.objects.create(title="Mathildefest", starts_on=today + timedelta(days=14))
    Event.objects.create(title="Gammel skovtur", starts_on=today - timedelta(days=400))
    NewsItem.objects.create(title="Åbent hus", body="<p>Kom forbi</p>", published_at=timezone.now())
    c = Client()

    page = c.get("/begivenheder/").content.decode()
    assert page.count("Mathildefest")  # upcoming event listed
    assert "Åbent hus" in page and "Kom forbi" in page  # news rendered
    assert "Gammel skovtur" in page  # past event listed

    home = c.get("/").content.decode()
    assert "Mathildefest" in home and "Åbent hus" in home  # forside teaser
    assert "/begivenheder/" in home  # link to the full page + nav


@pytest.mark.django_db
def test_statistik_renders_charts(make_resident: Callable) -> None:
    from datetime import date

    from stats.models import DailyVisitCount

    Application.objects.create(
        type=Application.Type.TOUR,
        full_name="A",
        email="a@x.dk",
        submitted_at=timezone.now(),
        university="DTU",
        heard_about_us="plakat",
    )
    DailyVisitCount.objects.create(date=date.today(), count=5)
    c = Client()
    c.force_login(make_resident(email="m@gahk.dk"))
    h = c.get("/intern/statistik/").content.decode()
    assert 'id="stats-data"' in h  # chart data embedded via json_script
    for cid in ("chart-applications", "chart-heard", "chart-visits"):
        assert f'id="{cid}"' in h  # the three chart canvases
    assert "Rundvisninger" in h and "plakat" in h  # labels present in the JSON payload


# ---------------------------------------------------------------- public/auth separation
@pytest.mark.django_db
def test_public_window_has_no_internal_tools() -> None:
    html = Client().get("/").content.decode()
    for label in ("Alumneliste", "AK-krydser", "Ølkælder-admin"):
        assert label not in html


@pytest.mark.django_db
def test_slashless_legacy_urls_redirect_instead_of_404() -> None:
    """The catch-all CMS pattern matches slashless paths owned by real apps, which suppresses
    Django's APPEND_SLASH. Those URLs are live on the PHP site (/optagelse returns 200) and
    bookmarked by residents (/nyintern), so they must 301 rather than 404 after the cutover."""
    from cms.models import Page

    c = Client()
    for path in ("/optagelse", "/nyintern", "/begivenheder"):
        r = c.get(path)
        assert r.status_code == 301, f"{path} should redirect, got {r.status_code}"
        assert r.headers["Location"] == f"{path}/"

    # The query string survives the redirect.
    r = c.get("/optagelse?type=fremleje")
    assert r.status_code == 301
    assert r.headers["Location"] == "/optagelse/?type=fremleje"

    # A real CMS page still renders directly, and a genuinely unknown slug still 404s.
    Page.objects.create(header="Faciliteter", slug="faciliteter", body="<p>hej</p>")
    assert c.get("/faciliteter").status_code == 200
    assert c.get("/findes-ikke").status_code == 404


@pytest.mark.django_db
def test_vaerelsestjek_is_open_to_every_resident(make_resident: Callable) -> None:
    """Room checks are done by whoever is around, so seeing and writing them is not gated on the
    inspektion embedsgruppe (F-005). akoverview stays AK-only — it is the AK group's own screen."""
    from core.models import Room
    from rooms.models import RoomCondition, RoomCriterion

    rm = Room.objects.create(legacy_index=91, number=91, floor="stuen", side="mod gaden")
    RoomCriterion.objects.create(code="vindue", name="Vindue", options=5)
    plain = make_resident(email="menig@gahk.dk")  # no roles at all
    c = Client()
    c.force_login(plain)

    assert c.get("/intern/vaerelsestjek/").status_code == 200
    assert c.get(f"/intern/vaerelsestjek/besvar/{rm.number}").status_code == 200

    c.post(f"/intern/vaerelsestjek/besvar/{rm.number}", {"score_vindue": "4", "comment_vindue": "Fin"})

    cond = RoomCondition.objects.get(room=rm, is_current=True)
    assert cond.resident == plain  # the writer is recorded, so the history stays attributable
    assert cond.scores.get(criterion__code="vindue").score == 4
    assert c.get(f"/intern/vaerelsestjek/se/{rm.number}").status_code == 200

    assert c.get("/intern/vaerelsestjek/akoverview").status_code == 403  # still AK-only
    nav = c.get("/intern/").context["nav_intern"]
    assert "Værelsestjek" in [label for _sec, items in nav for _u, label, _i in items]


@pytest.mark.django_db
def test_vaerelsestjek_still_requires_login() -> None:
    assert Client().get("/intern/vaerelsestjek/").status_code in (301, 302)


def test_room_criterion_score_scale_matches_legacy() -> None:
    """besvar.php:27-40 — options==3 → 0..2, options>2 → 1..options, otherwise 0..1.
    `options` selects the scale's *shape*; it is not a maximum."""
    from rooms.models import RoomCriterion

    assert RoomCriterion(options=5).score_values == [1, 2, 3, 4, 5]  # 13 real criteria
    assert RoomCriterion(options=3).score_values == [0, 1, 2]  # 9 real criteria
    assert RoomCriterion(options=2).score_values == [0, 1]  # 7 real criteria
    assert (RoomCriterion(options=3).score_min, RoomCriterion(options=3).score_max) == (0, 2)
    assert RoomCriterion(options=5).accepts_score(None)  # unanswered is fine
    assert not RoomCriterion(options=5).accepts_score(0)  # the 5-scale starts at 1
    assert not RoomCriterion(options=3).accepts_score(3)  # the 3-option scale tops out at 2


def test_parse_score_rejects_non_ascii_digits() -> None:
    """'²'.isdigit() is True but int('²') raises — the old guard 500'd on it."""
    from rooms.views import _parse_score

    assert _parse_score("3") == 3
    assert _parse_score("-2") == -2  # parses, then gets rejected by accepts_score
    assert _parse_score("") is None
    assert _parse_score("abc") is None
    assert _parse_score("²") is None
    assert _parse_score("9" * 5000) is None


@pytest.mark.django_db
def test_vaerelsestjek_shows_legend_and_drops_out_of_range(make_resident: Callable) -> None:
    """The per-criterion forklaring is rendered again, the widget offers the legacy scale, criteria
    come in legacy (code) order, and an off-scale score is dropped without losing the other answers."""
    from core.models import Room
    from rooms.models import RoomConditionScore, RoomCriterion

    rm = Room.objects.create(legacy_index=92, number=92, floor="stuen", side="mod gaden")
    RoomCriterion.objects.create(
        code="walls", name="Vægge", options=5, description="Maling.\r\n1: Nymalet/pæn stand,\r\n5: Huller"
    )
    RoomCriterion.objects.create(code="curtains", name="Gardiner", options=2, description="0: Er der")
    RoomCriterion.objects.create(code="contacts", name="Kontakter", options=3, description="0: Virker")
    c = Client()
    c.force_login(make_resident(email="tjek@gahk.dk"))

    html = c.get(f"/intern/vaerelsestjek/besvar/{rm.number}").content.decode()

    assert "Huller" in html  # the legend is back
    assert "Nymalet/pæn stand,<br>" in html  # multi-line legends keep their rungs
    assert 'max="5"' in html and 'max="2"' in html and 'max="1"' in html  # all three scale shapes
    assert 'min="1"' in html  # the 5-scale starts at 1, not 0
    # legacy order is by code; ordering by Danish name would put Gardiner first
    assert html.index("Kontakter") < html.index("Gardiner") < html.index("Vægge")

    r = c.post(
        f"/intern/vaerelsestjek/besvar/{rm.number}",
        {"score_walls": "9", "comment_walls": "Store revner", "score_curtains": "1"},
        follow=True,
    )
    walls = RoomConditionScore.objects.get(criterion__code="walls")
    assert walls.score is None  # the off-scale value was dropped …
    assert walls.comment == "Store revner"  # … but the observation survived
    assert RoomConditionScore.objects.get(criterion__code="curtains").score == 1
    assert "uden for skalaen" in r.content.decode()  # and the inspector was told


@pytest.mark.django_db
def test_vaerelsestjek_history_prefills_without_mutating_the_old_report(make_resident: Callable) -> None:
    """?rapport=<id> fills the form from an earlier report; submitting creates a NEW current one."""
    from core.models import Room
    from rooms.models import RoomCondition, RoomConditionScore, RoomCriterion

    rm = Room.objects.create(legacy_index=93, number=93, floor="1. sal", side="mod gaden")
    crit = RoomCriterion.objects.create(code="walls", name="Vægge", options=5)
    who = make_resident(email="tjek2@gahk.dk", first_name="Ida", last_name="Inspektør")
    old = RoomCondition.objects.create(
        room=rm, resident=who, recorded_by_name="Ida Inspektør", recorded_at=timezone.now(), is_current=False
    )
    RoomConditionScore.objects.create(condition=old, criterion=crit, score=2, comment="Gammel note")
    cur = RoomCondition.objects.create(
        room=rm, resident=who, recorded_by_name="Ida Inspektør", recorded_at=timezone.now(), is_current=True
    )
    RoomConditionScore.objects.create(condition=cur, criterion=crit, score=5, comment="Ny note")
    c = Client()
    c.force_login(who)

    assert 'value="5"' in c.get(f"/intern/vaerelsestjek/besvar/{rm.number}").content.decode()

    html = c.get(f"/intern/vaerelsestjek/besvar/{rm.number}?rapport={old.pk}").content.decode()
    assert 'value="2"' in html and "Gammel note" in html  # prefilled from the old report
    assert "tidligere rapport" in html  # …and says so

    c.post(f"/intern/vaerelsestjek/besvar/{rm.number}", {"score_walls": "3"})
    old.refresh_from_db()
    assert old.scores.get().score == 2  # the old report is untouched
    assert old.is_current is False
    assert RoomCondition.objects.filter(room=rm).count() == 3  # a new report, not an overwrite
    assert RoomCondition.objects.get(room=rm, is_current=True).scores.get().score == 3


@pytest.mark.django_db
def test_ak_overview_matrix_and_export_align_scores_with_headers(make_resident: Callable) -> None:
    """The legacy filled cells positionally from a delimited blob, so a room missing one criterion
    shifted every later score under the wrong heading. Keyed by criterion id, that cannot happen."""
    from core.models import Room
    from rooms.models import RoomCondition, RoomConditionScore, RoomCriterion

    rm = Room.objects.create(legacy_index=94, number=94, floor="2. sal", side="mod gården")
    a = RoomCriterion.objects.create(code="aaa", name="Alfa", options=5)
    RoomCriterion.objects.create(code="bbb", name="Beta", options=5)  # deliberately never scored
    z = RoomCriterion.objects.create(code="zzz", name="Zeta", options=5)
    cond = RoomCondition.objects.create(
        room=rm, recorded_by_name="Ida", recorded_at=timezone.now(), is_current=True
    )
    RoomConditionScore.objects.create(condition=cond, criterion=a, score=1)
    RoomConditionScore.objects.create(condition=cond, criterion=z, score=5)
    c = Client()
    c.force_login(make_resident(email="ak-matrix@gahk.dk", roles=("ak",)))

    html = c.get("/intern/vaerelsestjek/akoverview").content.decode()
    assert "Alfa" in html and "Beta" in html and "Zeta" in html

    csv_resp = c.get("/intern/vaerelsestjek/akoverview", {"format": "csv"})
    assert csv_resp["Content-Type"].startswith("text/csv")
    lines = csv_resp.content.decode("utf-8-sig").strip().splitlines()
    assert lines[0].split(",")[3:] == ["Alfa", "Beta", "Zeta"]
    # Zeta's 5 must land in Zeta's column, not shift left into the unscored Beta
    assert lines[1].split(",")[3:] == ["1", "", "5"]

    xlsx = c.get("/intern/vaerelsestjek/akoverview", {"format": "xlsx"})
    assert "spreadsheetml.sheet" in xlsx["Content-Type"]
    assert c.get("/intern/vaerelsestjek/akoverview").status_code == 200


@pytest.mark.django_db
def test_backfill_dedupes_on_naive_vs_aware_timestamps(make_resident: Callable) -> None:
    """The backfill keys on (room, recorded_at). MySQL yields naive datetimes and Django stores them
    aware, so without normalising first, every re-run duplicated all 620 historical reports."""
    from datetime import datetime

    from core.models import Room
    from rooms.models import RoomCondition

    rm = Room.objects.create(legacy_index=95, number=95, floor="3. sal", side="mod gaden")
    naive = datetime(2019, 5, 1, 12, 0)
    RoomCondition.objects.create(
        room=rm, recorded_by_name="Ida", recorded_at=timezone.make_aware(naive), is_current=False
    )

    seen = set(RoomCondition.objects.values_list("room_id", "recorded_at"))
    assert (rm.id, naive) not in seen  # the naive form does NOT match — this was the bug
    assert (rm.id, timezone.make_aware(naive)) in seen  # normalising first does


@pytest.mark.django_db
def test_vaerelsestjek_templates_leak_no_template_syntax(make_resident: Callable) -> None:
    """Django's {# … #} is single-line only — the lexer matches {#.*?#} without DOTALL, so a
    multi-line one is never tokenised and renders verbatim on the page. Use {% comment %} instead.
    Asserting presence (as the other tests do) cannot catch this; only asserting absence can."""
    from core.models import Room
    from rooms.models import RoomCondition, RoomConditionScore, RoomCriterion

    rm = Room.objects.create(legacy_index=96, number=96, floor="4. sal", side="mod gaden")
    crit = RoomCriterion.objects.create(code="walls", name="Vægge", options=5, description="1: Fin")
    cond = RoomCondition.objects.create(
        room=rm, recorded_by_name="Ida", recorded_at=timezone.now(), is_current=True
    )
    RoomConditionScore.objects.create(condition=cond, criterion=crit, score=3)
    c = Client()
    c.force_login(make_resident(email="leak@gahk.dk", roles=("ak",)))

    for path in (
        "/intern/vaerelsestjek/",
        f"/intern/vaerelsestjek/se/{rm.number}",
        f"/intern/vaerelsestjek/besvar/{rm.number}",
        "/intern/vaerelsestjek/akoverview",
    ):
        html = c.get(path).content.decode()
        for marker in ("{#", "#}", "{% comment", "{{", "{%"):
            assert marker not in html, f"{path} leaked template syntax: {marker}"


@pytest.mark.django_db
def test_vaerelsestjek_overview_renders_all_five_floor_plans(make_resident: Callable) -> None:
    """The picker regroups rooms by floor. dictsort cannot index a tuple by position — it returns an
    empty string on failure, so the whole block silently rendered nothing while the page still 200'd.
    Only asserting the plans are actually there catches that."""
    import re

    from core.management.commands.seed_rooms import Command as SeedRooms

    SeedRooms().handle()
    c = Client()
    c.force_login(make_resident(email="plan@gahk.dk"))

    html = c.get("/intern/vaerelsestjek/").content.decode()

    plans = re.findall(r'src="([^"]*image/intern/[^"]*)"', html)
    assert len(plans) == 5, f"expected five floor plans, got {plans}"
    assert [p.rsplit("/", 1)[-1].split(".")[0] for p in plans] == ["stuen", "sal1", "sal2", "sal3", "sal4"]
    assert "Stuen" in html and "4. sal" in html  # per-floor headings


def test_relocate_media_splits_multi_image_paths() -> None:
    """RoomConditionScore.image is a ';'-separated list; relocate_media treated the whole blob as one
    filename, so multi-image rows silently missed (copied 40 vs the real 5709). It must split the same
    way image_urls does, strip a leading slash, and skip URLs."""
    from core.management.commands.relocate_media import legacy_image_segments

    field = "public/image/a.jpg;/public/image/b.jpg;https://ex.com/c.jpg;"
    assert legacy_image_segments(field) == ["public/image/a.jpg", "public/image/b.jpg"]
    assert legacy_image_segments("") == []
    assert legacy_image_segments(None) == []
    assert legacy_image_segments("/public/image/x.jpg") == ["public/image/x.jpg"]  # leading slash


@pytest.mark.django_db
def test_relocate_media_segments_match_image_urls(make_resident: Callable) -> None:
    """The paths relocate_media copies to must be exactly the paths image_urls asks the browser for,
    or files land where nothing looks for them."""
    from core.management.commands.relocate_media import legacy_image_segments
    from core.models import Room
    from rooms.models import RoomCondition, RoomConditionScore, RoomCriterion

    rm = Room.objects.create(legacy_index=97, number=97, floor="stuen", side="mod gaden")
    crit = RoomCriterion.objects.create(code="floor", name="Gulve", options=5)
    cond = RoomCondition.objects.create(room=rm, recorded_at=timezone.now(), is_current=True)
    s = RoomConditionScore.objects.create(
        condition=cond, criterion=crit, image="public/image/a.jpg;/public/image/b.jpg"
    )

    assert s.image_urls == ["/media/public/image/a.jpg", "/media/public/image/b.jpg"]
    assert ["/media/" + seg for seg in legacy_image_segments(s.image)] == s.image_urls


@pytest.mark.django_db
def test_room_clash_warning_is_indstilling_only(make_resident: Callable) -> None:
    """A room clash is flagged on the alumneliste, but only to indstilling (who can fix it) — not to
    ordinary residents."""
    from core.models import Room
    from residents.models import Residency, active_period

    y, m = active_period()
    room = Room.objects.create(legacy_index=98, number=98, floor="stuen", side="mod gaden")
    a = make_resident(email="clash-a@gahk.dk")
    b = make_resident(email="clash-b@gahk.dk")
    Residency.objects.create(resident=a, room=room, year=y, month=m)
    Residency.objects.create(resident=b, room=room, year=y, month=m)  # same room, same month

    plain = Client()
    plain.force_login(a)
    assert "Værelseskonflikt" not in plain.get("/intern/alumneliste/").content.decode()

    ind = Client()
    ind.force_login(make_resident(email="clash-ind@gahk.dk", roles=("indstilling",)))
    html = ind.get("/intern/alumneliste/").content.decode()
    assert "Værelseskonflikt" in html and "098" in html


@pytest.mark.django_db
def test_cms_admin_link_in_sidebar_for_editor_roles(make_resident: Callable) -> None:
    """The 'Rediger indhold' sidebar link appears for content-editor roles and no one else."""
    for role in (Role.ADMINISTRATOR, Role.INDSTILLING, Role.INSPEKTION, Role.PR):
        c = Client()
        c.force_login(make_resident(email=f"navcms-{role.value}@gahk.dk", roles=[role]))
        nav = c.get("/intern/").context["nav_intern"]
        labels = [label for _sec, items in nav for _u, label, _i in items]
        assert "Rediger indhold" in labels, f"{role} should see the CMS link"

    plain = Client()
    plain.force_login(make_resident(email="navcms-plain@gahk.dk", roles=[Role.AK]))
    nav = plain.get("/intern/").context["nav_intern"]
    labels = [label for _sec, items in nav for _u, label, _i in items]
    assert "Rediger indhold" not in labels


@pytest.mark.django_db
def test_alumneliste_default_sort_is_alphabetical_and_sortable(make_resident: Callable) -> None:
    """Default order is by name (not room number), and columns can be sorted asc/desc (F-010)."""
    from core.models import Room
    from residents.models import Residency, active_period

    y, m = active_period()
    # Aaron in a high room, Zoe in a low room: name order and room order disagree.
    r_hi = Room.objects.create(legacy_index=201, number=201, floor="2. sal", side="mod gaden")
    r_lo = Room.objects.create(legacy_index=1, number=1, floor="stuen", side="mod gaden")
    Residency.objects.create(
        resident=make_resident(email="aaron@gahk.dk", first_name="Aaron", last_name="A"),
        room=r_hi,
        year=y,
        month=m,
    )
    Residency.objects.create(
        resident=make_resident(email="zoe@gahk.dk", first_name="Zoe", last_name="Z"),
        room=r_lo,
        year=y,
        month=m,
    )
    c = Client()
    c.force_login(make_resident(email="viewer-sort@gahk.dk"))

    default = c.get("/intern/alumneliste/").content.decode()
    assert default.index("Aaron A") < default.index("Zoe Z")  # alphabetical, not by room (201 vs 001)

    by_room = c.get("/intern/alumneliste/", {"sort": "vaerelse", "dir": "asc"}).content.decode()
    assert by_room.index("Zoe Z") < by_room.index("Aaron A")  # room 001 before 201

    name_desc = c.get("/intern/alumneliste/", {"sort": "navn", "dir": "desc"}).content.decode()
    assert name_desc.index("Zoe Z") < name_desc.index("Aaron A")  # reversed

    bad = c.get("/intern/alumneliste/", {"sort": "'; DROP", "dir": "sideways"}).content.decode()
    assert bad.index("Aaron A") < bad.index("Zoe Z")  # junk sort falls back to name asc, no crash

    # No unrendered Django template syntax leaks onto the page (a multi-line {# #} would render verbatim).
    assert "{#" not in default and "{%" not in default


@pytest.mark.django_db
def test_directory_rows_fragment_has_sortable_headers(make_resident: Callable) -> None:
    """The htmx fragment is the whole table (headers + rows) so sort arrows update on swap."""
    from core.models import Room
    from residents.models import Residency, active_period

    y, m = active_period()
    room = Room.objects.create(legacy_index=1, number=1, floor="stuen", side="mod gaden")
    Residency.objects.create(
        resident=make_resident(email="frag@gahk.dk", first_name="Frag", last_name="Menter"),
        room=room,
        year=y,
        month=m,
    )
    c = Client()
    c.force_login(make_resident(email="viewer-frag@gahk.dk"))

    html = c.get("/intern/alumneliste/rows", {"sort": "navn", "dir": "asc"}).content.decode()
    assert "Frag Menter" in html  # rows present
    assert "sort=vaerelse" in html  # a sortable header link is present
    assert 'name="sort"' in html and 'value="navn"' in html  # hidden sort state for the search box
