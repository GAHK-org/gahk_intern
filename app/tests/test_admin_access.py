"""The two-person rule on Django-admin rights (residents.admin_access)."""

from collections.abc import Callable

import pytest
from django.core import mail
from django.test import Client
from django.urls import reverse

from residents.admin_access import can_decide, decide
from residents.models import AdminAccessGrant, Resident, Role, RoleAssignment, active_period


def _grant(resident: Resident) -> AdminAccessGrant:
    return AdminAccessGrant.objects.get(resident=resident)


def _approved_admin(make_resident: Callable, email: str) -> Resident:
    r = make_resident(email=email, roles=[Role.ADMINISTRATOR])
    AdminAccessGrant.objects.filter(resident=r).update(status=AdminAccessGrant.Status.APPROVED)
    return Resident.objects.get(pk=r.pk)


@pytest.mark.django_db
def test_new_administrator_has_no_admin_access_until_approved(make_resident: Callable) -> None:
    newcomer = make_resident(email="ny@gahk.dk", roles=[Role.ADMINISTRATOR])
    assert _grant(newcomer).status == AdminAccessGrant.Status.PENDING
    assert newcomer.has_perm("residents.delete_resident") is False

    approver = _approved_admin(make_resident, "gammel@gahk.dk")
    decide(_grant(newcomer), approver, approve=True)
    assert Resident.objects.get(pk=newcomer.pk).has_perm("residents.delete_resident") is True


@pytest.mark.django_db
def test_approving_requires_a_second_person(make_resident: Callable) -> None:
    granter = _approved_admin(make_resident, "granter@gahk.dk")
    newcomer = make_resident(email="ny2@gahk.dk", roles=[Role.ADMINISTRATOR])
    grant = _grant(newcomer)
    grant.requested_by = granter
    grant.save(update_fields=["requested_by"])

    assert can_decide(newcomer, grant) is False  # not your own
    assert can_decide(granter, grant) is False  # not the one who granted the role
    assert can_decide(_approved_admin(make_resident, "tredje@gahk.dk"), grant) is True


@pytest.mark.django_db
def test_unapproved_administrator_cannot_approve(make_resident: Callable) -> None:
    """Otherwise two pending newcomers could let each other in."""
    a = make_resident(email="a@netvaerk.dk", roles=[Role.ADMINISTRATOR])
    b = make_resident(email="b@netvaerk.dk", roles=[Role.ADMINISTRATOR])
    assert can_decide(a, _grant(b)) is False


@pytest.mark.django_db
def test_superuser_can_approve_the_first_grant(make_resident: Callable) -> None:
    su = make_resident(email="su2@gahk.dk", is_superuser=True, is_staff=True)
    first = make_resident(email="foerste@gahk.dk", roles=[Role.ADMINISTRATOR])
    assert can_decide(su, _grant(first)) is True


@pytest.mark.django_db
def test_approvers_are_mailed_when_a_grant_opens(
    make_resident: Callable, django_capture_on_commit_callbacks: Callable
) -> None:
    _approved_admin(make_resident, "modtager@gahk.dk")
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        make_resident(email="ny3@gahk.dk", roles=[Role.ADMINISTRATOR], first_name="Nynne")
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["modtager@gahk.dk"]  # not the newcomer
    assert "Nynne" in mail.outbox[0].body
    assert reverse("siteadmin:admin_access") in mail.outbox[0].body


@pytest.mark.django_db
def test_monthly_roll_forward_does_not_reopen_an_approved_grant(make_resident: Callable) -> None:
    """A continuing administrator gets a fresh RoleAssignment every month; re-approving them twelve
    times a year would train the group to click accept without reading."""
    admin = _approved_admin(make_resident, "fortsaetter@gahk.dk")
    y, m = active_period()
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    RoleAssignment.objects.create(resident=admin, role=Role.ADMINISTRATOR, year=ny, month=nm)
    assert _grant(admin).status == AdminAccessGrant.Status.APPROVED


@pytest.mark.django_db
def test_leaving_netvaerk_entirely_requires_a_fresh_approval(make_resident: Callable) -> None:
    admin = _approved_admin(make_resident, "forlader@gahk.dk")
    RoleAssignment.objects.filter(resident=admin, role=Role.ADMINISTRATOR).delete()
    assert AdminAccessGrant.objects.filter(resident=admin).exists() is False

    y, m = active_period()
    RoleAssignment.objects.create(resident=admin, role=Role.ADMINISTRATOR, year=y, month=m)
    assert _grant(admin).status == AdminAccessGrant.Status.PENDING
    assert Resident.objects.get(pk=admin.pk).has_perm("residents.delete_resident") is False


@pytest.mark.django_db
def test_decide_view_refuses_a_grant_the_user_may_not_decide(client: Client, make_resident: Callable) -> None:
    newcomer = make_resident(email="ny4@gahk.dk", roles=[Role.ADMINISTRATOR], password="pw")
    client.login(username="ny4@gahk.dk", password="pw")
    resp = client.post(reverse("siteadmin:admin_access"), {"grant": _grant(newcomer).id, "action": "approve"})
    assert resp.status_code == 302
    assert _grant(newcomer).status == AdminAccessGrant.Status.PENDING  # own grant, refused


def _live_here(resident: Resident) -> None:
    """Put `resident` on the active month's list — the residents the roles screen shows."""
    from core.models import Room
    from residents.models import Residency

    y, m = active_period()
    room = Room.objects.create(legacy_index=100 + resident.pk, number=100 + resident.pk, floor="1", side="x")
    Residency.objects.create(resident=resident, room=room, year=y, month=m)


# ---- the Django-admin Resident form (privilege escalation) ------------------------------------------------


@pytest.mark.django_db
def test_resident_admin_form_offers_no_superuser_groups_permissions_or_hash(make_resident: Callable) -> None:
    admin = _approved_admin(make_resident, "admin-form@gahk.dk")
    target = make_resident(email="target@gahk.dk")
    c = Client()
    c.force_login(admin)
    url = reverse("admin:residents_resident_change", args=[target.pk])

    page = c.get(url).content.decode()
    for field in ("is_superuser", "groups", "user_permissions", "password"):
        assert f'name="{field}"' not in page, field
    assert target.password not in page  # not even read-only
    assert 'name="first_name"' in page  # the ordinary fields are still editable

    # Posting the fields anyway changes nothing: the form does not know them.
    data = {
        "email": target.email,
        "first_name": "Omdøbt",
        "last_name": "Beboer",
        "is_active": "on",
        "is_superuser": "on",
        "password": "pbkdf2_sha256$1$x$y",
    }
    assert c.post(url, data).status_code == 302
    target.refresh_from_db()
    assert target.first_name == "Omdøbt"
    assert target.is_superuser is False
    assert target.check_password("hemmelig")


@pytest.mark.django_db
def test_an_administrator_cannot_edit_or_delete_a_superuser(make_resident: Callable) -> None:
    from django.contrib.admin.sites import site
    from django.test import RequestFactory

    admin = _approved_admin(make_resident, "admin-su@gahk.dk")
    su = Resident.objects.create_superuser(email="su2@gahk.dk", password="pw", first_name="S", last_name="U")
    model_admin = site._registry[Resident]
    request = RequestFactory().get("/")
    request.user = admin
    assert model_admin.has_change_permission(request, su) is False
    assert model_admin.has_delete_permission(request, su) is False
    assert model_admin.has_change_permission(request, make_resident(email="plain-su@gahk.dk")) is True
    request.user = su
    assert model_admin.has_change_permission(request, su) is True


# ---- the roles screen -------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_roles_screen_only_assigns_to_residents_of_the_listed_month(make_resident: Callable) -> None:
    admin = _approved_admin(make_resident, "roles@gahk.dk")
    resident = make_resident(email="bor-her@gahk.dk")
    _live_here(resident)
    moved_out = make_resident(email="flyttet@gahk.dk")  # no residency in the active month
    c = Client()
    c.force_login(admin)
    url = reverse("siteadmin:roles")

    c.post(url, {"resident": moved_out.pk, "role": Role.ADMINISTRATOR, "action": "add"})
    assert not RoleAssignment.objects.filter(resident=moved_out).exists()
    assert c.post(url, {"resident": "abc", "role": Role.AK, "action": "add"}).status_code == 302  # not a 500

    c.post(url, {"resident": resident.pk, "role": Role.AK, "action": "add"})
    assert RoleAssignment.objects.filter(resident=resident, role=Role.AK).exists()


# ---- a refused grant is reopened by a fresh grant ---------------------------------------------------------


@pytest.mark.django_db
def test_granting_administrator_again_reopens_a_refused_grant(
    make_resident: Callable, django_capture_on_commit_callbacks: Callable
) -> None:
    granter = _approved_admin(make_resident, "giver@gahk.dk")
    refused = make_resident(email="afvist@gahk.dk", roles=[Role.ADMINISTRATOR])
    _live_here(refused)
    decide(_grant(refused), granter, approve=False)
    assert _grant(refused).status == AdminAccessGrant.Status.DENIED

    # The monthly roll-forward is not a new request: a refusal must not come back every month.
    y, m = active_period()
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    RoleAssignment.objects.create(resident=refused, role=Role.ADMINISTRATOR, year=ny, month=nm)
    assert _grant(refused).status == AdminAccessGrant.Status.DENIED

    # Taking the role away for this month and granting it again in the role editor is.
    c = Client()
    c.force_login(granter)
    url = reverse("siteadmin:roles")
    c.post(url, {"resident": refused.pk, "role": Role.ADMINISTRATOR, "action": "remove"})
    assert _grant(refused).status == AdminAccessGrant.Status.DENIED  # still held next month
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        c.post(url, {"resident": refused.pk, "role": Role.ADMINISTRATOR, "action": "add"})
    grant = _grant(refused)
    assert grant.status == AdminAccessGrant.Status.PENDING
    assert grant.requested_by == granter and grant.decided_by is None and grant.decided_at is None
    assert len(mail.outbox) == 1  # the approvers hear about it again
    assert can_decide(granter, grant) is False  # the granter still cannot approve their own request
