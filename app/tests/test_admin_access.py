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
