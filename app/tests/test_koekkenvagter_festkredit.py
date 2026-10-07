"""Køkkenvagter Amendment 6 -- festkredit (party credit). Design: `docs/plans/2026-10-07-koekkenvagter-a6-design.md`.

Dates are fixed in the past (the event month 2025-12) and `today` is passed explicitly to the services, so
nothing here depends on the wall clock; the view tests use the real clock, which is always after 2025-12.
"""

from collections.abc import Callable
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.db.models import ProtectedError, Sum
from django.test import Client
from django.utils import timezone

from core.models import PushSubscription, Room
from koekken import demo
from koekken.models import FestKredit, KoekkenPost, Vagt, VagtRegel
from koekken.services import (
    KoekkenAllocationError,
    _tier_a_sort_key,
    award_festkredit,
    balance_for,
    bulk_projected_balances,
    charged_residents,
    festkredit_history,
    festkredit_preview,
    post_obligation,
    recent_posts,
    resolve_periode,
    split_evenly,
    undo_festkredit,
)
from residents.models import Residency, Resident, Role

pytestmark = pytest.mark.django_db

TODAY = date(2026, 10, 7)
EVENT = date(2025, 12, 31)
_room_seq = iter(range(1, 30_000))
_email_seq = iter(range(10_000))


def _room() -> Room:
    n = next(_room_seq)
    return Room.objects.create(legacy_index=n, number=n, floor="stuen", side="mod gaden")


def _house(make_resident: Callable, n: int, *, year: int = 2025, month: int = 12) -> list[Resident]:
    """`n` residents, all on the (year, month) list."""
    residents = []
    for _ in range(n):
        r = make_resident(email=f"fest{next(_email_seq)}@gahk.dk")
        Residency.objects.create(resident=r, room=_room(), year=year, month=month)
        residents.append(r)
    return residents


def _total_ledger() -> int:
    return KoekkenPost.objects.aggregate(t=Sum("delta_minutes"))["t"] or 0


def _snapshot() -> dict[int, int]:
    return {r.pk: balance_for(r) for r in Resident.objects.all()}


def _award(by: Resident, helpers: dict[Resident, int], **kw: object) -> FestKredit:
    return award_festkredit(
        "Nytårsfest", EVENT, {r.pk: h for r, h in helpers.items()}, by=by, today=TODAY, **kw
    )  # type: ignore[arg-type]


# ----------------------------------------------------------------------------------- split helpers


def test_split_evenly_exact_division(make_resident: Callable) -> None:
    rs = _house(make_resident, 4)
    assert [a for _, a in split_evenly(400, rs)] == [100, 100, 100, 100]


def test_split_evenly_remainder_goes_to_lowest_pk_and_sums_exactly(make_resident: Callable) -> None:
    rs = _house(make_resident, 61)
    parts = split_evenly(45 * 60, rs)  # 2700 / 61 = 44 rem 16
    assert sum(a for _, a in parts) == 2700
    assert [a for _, a in parts[:16]] == [45] * 16
    assert [a for _, a in parts[16:]] == [44] * 45
    assert [r.pk for r, _ in parts] == [r.pk for r in rs]


def test_split_evenly_single_resident(make_resident: Callable) -> None:
    (r,) = _house(make_resident, 1)
    assert split_evenly(137, [r]) == [(r, 137)]


# ------------------------------------------------------------------------------------------ award


def test_award_writes_credit_and_funding_and_ledger_stays_zero(make_resident: Callable) -> None:
    house = _house(make_resident, 61)
    officer = house[0]
    helpers = {
        house[1]: 5,
        house[2]: 5,
        house[3]: 5,
        house[4]: 6,
        house[5]: 4,
        house[6]: 5,
        house[7]: 5,
        house[8]: 5,
    }
    before = _snapshot()
    award = _award(officer, helpers)

    credit = KoekkenPost.objects.filter(festkredit=award, kind=KoekkenPost.Kind.FESTKREDIT)
    assert {p.resident_id: p.delta_minutes for p in credit} == {r.pk: h * 60 for r, h in helpers.items()}
    total = sum(helpers.values()) * 60  # 40 h = 2400 min, 2400 / 61 does not divide
    assert total % 61 != 0
    funding = KoekkenPost.objects.filter(festkredit=award, kind=KoekkenPost.Kind.FESTBIDRAG)
    assert {p.resident_id for p in funding} == {r.pk for r in charged_residents(2025, 12, today=TODAY)}
    assert sum(p.delta_minutes for p in funding) == -total
    assert max(-p.delta_minutes for p in funding) - min(-p.delta_minutes for p in funding) == 1
    assert _total_ledger() == 0
    for p in KoekkenPost.objects.filter(festkredit=award):
        assert p.month is None and p.vagt_id is None
        assert p.periode == resolve_periode(EVENT)
        assert p.created_by == officer
    assert award.created_by == officer and award.fortrudt_at is None
    after = _snapshot()
    assert sum(after.values()) - sum(before.values()) == 0


def test_preview_matches_what_is_written(make_resident: Callable) -> None:
    house = _house(make_resident, 7)
    timer = {house[1].pk: 3, house[2].pk: 2}
    preview = festkredit_preview("Fest", EVENT, timer, today=TODAY)
    assert preview.total_minutes == 300
    assert [r.pk for r in preview.charged] == [r.pk for r in house]
    assert preview.per_resident_minutes == (42, 43)  # 300 / 7 = 42 rem 6
    assert preview.month_label == "December 2025"
    assert {r.pk: h for r, h in preview.helpers} == timer
    assert KoekkenPost.objects.count() == 0 and FestKredit.objects.count() == 0  # pure

    award = award_festkredit("Fest", EVENT, timer, by=house[0], today=TODAY)
    funding = [-p.delta_minutes for p in KoekkenPost.objects.filter(festkredit=award, kind="festbidrag")]
    assert (min(funding), max(funding)) == preview.per_resident_minutes


def test_only_helpers_are_pushed_via_allowed_subscribers(make_resident: Callable) -> None:
    house = _house(make_resident, 5)
    for r in house:
        PushSubscription.objects.create(
            user=r, endpoint=f"https://push.example/{r.pk}", p256dh="k", auth="a", wants_koekken=True
        )
    with (
        patch("core.push.send") as send,
        patch("koekken.access.allowed_subscribers", side_effect=lambda qs: qs),
    ):
        with patch("django.db.transaction.on_commit", side_effect=lambda f: f()):
            _award(house[0], {house[1]: 5})
    assert send.call_count == 1
    audience, _head, body, _url = send.call_args.args
    assert list(audience.values_list("user_id", flat=True)) == [house[1].pk]
    assert body == "Du har fået 5 t køkkenkredit for Nytårsfest."


# ---------------------------------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    ("navn", "dato", "hours", "message"),
    [
        ("Fest", TODAY + timedelta(days=1), 5, "Datoen ligger i fremtiden"),
        ("   ", EVENT, 5, "navn"),
        ("Fest", EVENT, 0, "mindst 1 time"),
        ("Fest", EVENT, -3, "mindst 1 time"),
    ],
)
def test_refusals_write_nothing(
    make_resident: Callable, navn: str, dato: date, hours: int, message: str
) -> None:
    house = _house(make_resident, 3)
    with pytest.raises(KoekkenAllocationError, match=message):
        award_festkredit(navn, dato, {house[1].pk: hours}, by=house[0], today=TODAY)
    assert KoekkenPost.objects.count() == 0 and FestKredit.objects.count() == 0


def test_no_helpers_refused(make_resident: Callable) -> None:
    house = _house(make_resident, 3)
    with pytest.raises(KoekkenAllocationError, match="mindst én hjælper"):
        award_festkredit("Fest", EVENT, {}, by=house[0], today=TODAY)
    assert FestKredit.objects.count() == 0


def test_moved_out_helper_refused(make_resident: Callable) -> None:
    house = _house(make_resident, 3)
    Resident.objects.filter(pk=house[1].pk).update(move_out_date=TODAY - timedelta(days=1))
    house[1].refresh_from_db()
    with pytest.raises(KoekkenAllocationError, match="er fraflyttet"):
        award_festkredit("Fest", EVENT, {house[1].pk: 2}, by=house[0], today=TODAY)
    assert KoekkenPost.objects.count() == 0 and FestKredit.objects.count() == 0


def test_month_without_published_list_refused_naming_the_month(make_resident: Callable) -> None:
    house = _house(make_resident, 3)
    with pytest.raises(KoekkenAllocationError, match="ingen offentliggjort alumneliste for Juli 2025"):
        award_festkredit("Fest", date(2025, 7, 10), {house[1].pk: 2}, by=house[0], today=TODAY)
    assert KoekkenPost.objects.count() == 0 and FestKredit.objects.count() == 0


# ------------------------------------------------------------------------------------- funding set


def test_funding_excludes_moved_out_and_helper_off_list_pays_nothing(make_resident: Callable) -> None:
    house = _house(make_resident, 4)
    Resident.objects.filter(pk=house[3].pk).update(move_out_date=TODAY - timedelta(days=1))
    newcomer = make_resident(email="newcomer@gahk.dk")  # no Residency row for the event month
    assert [r.pk for r in charged_residents(2025, 12, today=TODAY)] == [r.pk for r in house[:3]]

    award = _award(house[0], {newcomer: 3})
    funding = KoekkenPost.objects.filter(festkredit=award, kind="festbidrag")
    assert {p.resident_id for p in funding} == {r.pk for r in house[:3]}
    assert balance_for(newcomer) == 180
    assert _total_ledger() == 0


# ---------------------------------------------------------------------------------------- undo


def test_undo_restores_balances_and_second_undo_refused(make_resident: Callable) -> None:
    house = _house(make_resident, 9)
    other = _house(make_resident, 1, month=11)[0]
    KoekkenPost.objects.create(
        resident=house[1], delta_minutes=-90, kind="justering", periode=resolve_periode(EVENT)
    )
    before = _snapshot()
    award = _award(house[0], {house[1]: 5, house[2]: 2})
    # A helper who moves out afterwards is still reversed (a correction, not a new entry).
    Resident.objects.filter(pk=house[2].pk).update(move_out_date=TODAY - timedelta(days=1))

    with patch("core.push.send") as send:
        undone = undo_festkredit(award, by=other)
    send.assert_not_called()
    assert _snapshot() == before
    originals = KoekkenPost.objects.filter(festkredit=award, kind__in=["festkredit", "festbidrag"])
    reversals = KoekkenPost.objects.filter(festkredit=award, kind="tilbagefoersel")
    assert reversals.count() == originals.count() == 2 + 9
    assert sorted((p.resident_id, p.delta_minutes) for p in reversals) == sorted(
        (p.resident_id, -p.delta_minutes) for p in originals
    )
    assert undone.fortrudt_at is not None and undone.fortrudt_by == other
    award.refresh_from_db()
    assert award.fortrudt_at is not None

    with pytest.raises(KoekkenAllocationError, match="allerede fortrudt"):
        undo_festkredit(award, by=other)
    assert KoekkenPost.objects.filter(kind="tilbagefoersel").count() == 11


def test_award_is_protected_from_deletion(make_resident: Callable) -> None:
    house = _house(make_resident, 3)
    award = _award(house[0], {house[1]: 1})
    with pytest.raises(ProtectedError):
        award.delete()


# ------------------------------------------------------------------------------ post_obligation


def test_post_obligation_rerun_leaves_festkredit_rows_alone(make_resident: Callable) -> None:
    house = _house(make_resident, 5)
    periode = resolve_periode(EVENT)
    Vagt.objects.create(
        periode=periode,
        date=date(2025, 12, 10),
        kind=VagtRegel.Kind.MORGEN,
        headcount=1,
        duration_minutes=100,
    )
    award = _award(house[0], {house[1]: 2})
    count = KoekkenPost.objects.filter(festkredit=award).count()
    post_obligation(periode, 12)
    # Drop a resident from the list so the stale-row delete actually runs, then re-run.
    Residency.objects.filter(resident=house[4]).delete()
    post_obligation(periode, 12)
    assert KoekkenPost.objects.filter(festkredit=award).count() == count
    assert KoekkenPost.objects.filter(kind="festbidrag", resident=house[4]).count() == 1
    # The stale FORPLIGTELSE row really was removed, while the festkredit row survived.
    assert not KoekkenPost.objects.filter(kind="forpligtelse", resident=house[4]).exists()
    assert KoekkenPost.objects.filter(kind="festbidrag", resident=house[4], festkredit=award).exists()


# --------------------------------------------------------------------------------------- ranking


def test_helper_ranks_after_identical_non_helper_and_funding_alone_does_not_reorder(
    make_resident: Callable,
) -> None:
    house = _house(make_resident, 5)
    a, b, helper = house[1], house[2], house[3]
    declared: dict[int, date] = {}
    before = bulk_projected_balances(house)
    order_before = sorted(house, key=lambda r: _tier_a_sort_key(r, before, declared))

    _award(house[0], {helper: 4})
    after = bulk_projected_balances(house)
    ranked = sorted(house, key=lambda r: _tier_a_sort_key(r, after, declared))
    assert ranked.index(helper) > ranked.index(a)
    assert ranked.index(helper) > ranked.index(b)
    # The funding debit is the same constant for everyone charged: non-helpers keep their relative order.
    non_helpers = [r for r in order_before if r != helper]
    assert [r for r in ranked if r != helper] == non_helpers


# --------------------------------------------------------------------------------- history / card


def test_history_query_count_is_constant(
    make_resident: Callable, django_assert_num_queries: Callable
) -> None:
    house = _house(make_resident, 4)
    _award(house[0], {house[1]: 2})
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as one:
        rows = festkredit_history()
    assert len(rows) == 1 and rows[0].total_hours == 2
    for i in range(4):
        _award(house[0], {house[1]: 1, house[2]: 1 + i})
    with CaptureQueriesContext(connection) as many:
        rows = festkredit_history()
    assert len(rows) == 5
    assert len(many) == len(one)
    assert rows[0].helpers  # newest first, helpers with hours


def test_recent_posts_labels_limit_and_order(make_resident: Callable) -> None:
    house = _house(make_resident, 3)
    me = house[1]
    periode = resolve_periode(EVENT)
    now = timezone.now()
    vagt = Vagt.objects.create(
        periode=periode, date=date(2025, 12, 3), kind=VagtRegel.Kind.AFTEN, headcount=2, duration_minutes=120
    )

    def post(kind: str, delta: int, minutes_ago: int, **kw: object) -> KoekkenPost:
        return KoekkenPost.objects.create(
            resident=me, delta_minutes=delta, kind=kind, periode=periode,
            created_at=now - timedelta(minutes=minutes_ago), **kw,
        )  # fmt: skip

    post("justering", 5, 110)  # the 11th-newest: falls outside the limit of 10
    post("startsaldo", 60, 100)
    post("justering", 15, 90)
    post("forpligtelse", -200, 80, month=12)
    post("arbejde", 120, 70, vagt=vagt)
    post("arbejde", 30, 60)
    post("tilbagefoersel", -120, 50, vagt=vagt)
    award = _award(house[0], {me: 3})
    undo_festkredit(award, by=house[0])
    labels = [label for _, label in recent_posts(me)]
    assert labels == [
        "Fest: Nytårsfest",  # tilbagefoersel x2 (newest), festbidrag, festkredit
        "Fest: Nytårsfest",
        "Fest: Nytårsfest",
        "Fest: Nytårsfest",
        f"Tilbageført: {vagt}",
        "Udført arbejde",
        f"Udført: {vagt}",
        "Forpligtelse december",
        "Manuel justering",
        "Startsaldo",
    ]
    assert len(recent_posts(me, limit=3)) == 3


# ------------------------------------------------------------------------------------------ views

BASE = "/intern/koekken/gruppe/festkredit/"


def _manager(make_resident: Callable) -> Resident:
    """Køkkengruppen. Created AFTER the house: a role is assigned for the active period, which follows the
    latest `Residency` list, so a list created afterwards would move the period out from under the role."""
    return make_resident(email="festmanager@gahk.dk", roles=(Role.KOKKENGRUPPE,))


def test_non_manager_gets_403_everywhere_and_sees_no_link(make_resident: Callable, client: Client) -> None:
    house = _house(make_resident, 3)
    award = _award(house[0], {house[1]: 1})
    client.force_login(house[2])
    assert client.get(BASE).status_code == 403
    assert client.post(BASE, {"step": "preview"}).status_code == 403
    assert client.post(f"{BASE}{award.pk}/fortryd").status_code == 403
    award.refresh_from_db()
    assert award.fortrudt_at is None
    assert "/gruppe/festkredit/" not in client.get("/intern/koekken/").content.decode()
    assert client.get("/intern/koekken/gruppe/").status_code == 403


def test_manager_sees_link_on_gruppe_page(make_resident: Callable, client: Client) -> None:
    manager = _manager(make_resident)
    client.force_login(manager)
    assert BASE in client.get("/intern/koekken/gruppe/").content.decode()


def test_form_confirm_award_round_trip_with_edited_hours(make_resident: Callable, client: Client) -> None:
    house = _house(make_resident, 6)
    manager = _manager(make_resident)
    client.force_login(manager)
    assert client.get(BASE).status_code == 200

    response = client.post(
        BASE,
        {
            "step": "preview",
            "navn": "Nytårsfest",
            "dato": "2025-12-31",
            "hjaelpere": [house[0].pk, house[1].pk],
            "timer": 4,
        },
    )
    html = response.content.decode()
    assert response.status_code == 200
    assert f'name="timer_{house[0].pk}"' in html and f'name="timer_{house[1].pk}"' in html
    assert "I alt 8 t." in html and "6 beboere" in html and "december 2025" in html
    assert FestKredit.objects.count() == 0  # nothing written until "Tildel"

    response = client.post(
        BASE,
        {
            "step": "tildel",
            "navn": "Nytårsfest",
            "dato": "2025-12-31",
            f"timer_{house[0].pk}": 6,
            f"timer_{house[1].pk}": 2,
        },
        follow=True,
    )
    assert "Festkredit for Nytårsfest tildelt 2 hjælpere." in response.content.decode()
    credit = {p.resident_id: p.delta_minutes for p in KoekkenPost.objects.filter(kind="festkredit")}
    assert credit == {house[0].pk: 360, house[1].pk: 120}
    assert _total_ledger() == 0
    assert "Nytårsfest" in response.content.decode() and "Fortryd tildeling" in response.content.decode()


def test_refusal_rerenders_confirm_step_with_error(make_resident: Callable, client: Client) -> None:
    house = _house(make_resident, 3)
    manager = _manager(make_resident)
    client.force_login(manager)
    future = (timezone.localdate() + timedelta(days=3)).isoformat()
    response = client.post(
        BASE, {"step": "tildel", "navn": "Fest", "dato": future, f"timer_{house[0].pk}": 2}
    )
    html = response.content.decode()
    assert response.status_code == 200
    assert "Datoen ligger i fremtiden" in html and f'name="timer_{house[0].pk}"' in html
    assert KoekkenPost.objects.count() == 0

    # The same refusal on the preview step re-renders the form step.
    response = client.post(
        BASE, {"step": "preview", "navn": "Fest", "dato": future, "hjaelpere": [house[0].pk], "timer": 2}
    )
    assert "Datoen ligger i fremtiden" in response.content.decode()
    assert 'name="step" value="preview"' in response.content.decode()
    # The typed date survives the re-render (a bound form's value is a raw string).
    assert f'name="dato" value="{future}"' in response.content.decode()


def test_form_deduplicates_repeated_helpers(make_resident: Callable) -> None:
    from koekken.forms import FestKreditForm

    house = _house(make_resident, 3)
    form = FestKreditForm(
        {
            "navn": "Fest",
            "dato": "2025-12-31",
            "hjaelpere": [house[0].pk, house[0].pk, house[1].pk],
            "timer": 2,
        }
    )
    assert form.is_valid(), form.errors
    assert sorted(r.pk for r in form.cleaned_data["hjaelpere"]) == sorted([house[0].pk, house[1].pk])


def test_history_shows_fortrudt_without_undo_button_and_undo_via_view(
    make_resident: Callable, client: Client
) -> None:
    house = _house(make_resident, 3)
    manager = _manager(make_resident)
    live = _award(house[0], {house[1]: 1})
    dead = _award(house[0], {house[1]: 2})
    undo_festkredit(dead, by=manager)
    client.force_login(manager)
    html = client.get(BASE).content.decode()
    assert "Fortrudt" in html
    assert f"{BASE}{live.pk}/fortryd" in html
    assert f"{BASE}{dead.pk}/fortryd" not in html
    assert html.count("Fortryd tildeling") == 1

    response = client.post(f"{BASE}{live.pk}/fortryd", follow=True)
    assert "er fortrudt" in response.content.decode()
    response = client.post(f"{BASE}{live.pk}/fortryd", follow=True)
    assert "allerede fortrudt" in response.content.decode()
    assert client.post(f"{BASE}999999/fortryd").status_code == 404
    assert client.get(f"{BASE}{live.pk}/fortryd").status_code == 405


def test_resident_card_shows_posteringer(make_resident: Callable, client: Client) -> None:
    house = _house(make_resident, 3)
    _award(house[0], {house[1]: 5})
    client.force_login(house[1])
    html = client.get("/intern/koekken/").content.decode()
    assert "Seneste posteringer" in html
    assert html.count("Fest: Nytårsfest") == 2
    assert "+5,0 t" in html
    client.force_login(_house(make_resident, 1, month=1)[0])
    assert "Ingen posteringer endnu." in client.get("/intern/koekken/").content.decode()


# ---------------------------------------------------------------------------------- admin / demo


def test_admin_changelist_loads_and_delete_with_posts_is_blocked(
    make_resident: Callable, client: Client
) -> None:
    house = _house(make_resident, 3)
    award = _award(house[0], {house[1]: 1})
    admin_user = make_resident(email="festadmin@gahk.dk", is_superuser=True, is_staff=True)
    client.force_login(admin_user)
    assert client.get("/django-admin/koekken/festkredit/").status_code == 200
    assert client.get(f"/django-admin/koekken/festkredit/{award.pk}/change/").status_code == 200
    response = client.post(f"/django-admin/koekken/festkredit/{award.pk}/delete/", {"post": "yes"})
    assert FestKredit.objects.filter(pk=award.pk).exists()
    assert b"kan ikke slette" in response.content.lower()


def test_demo_produces_one_live_and_one_undone_award(make_resident: Callable) -> None:
    house = _house(make_resident, 10, year=TODAY.year, month=TODAY.month)
    demo._demo_festkredit(house, TODAY)
    live = FestKredit.objects.filter(fortrudt_at__isnull=True)
    undone = FestKredit.objects.filter(fortrudt_at__isnull=False)
    assert live.count() == 1 and undone.count() == 1
    assert KoekkenPost.objects.filter(festkredit=live[0], kind="festkredit").count() == 8
    assert _total_ledger() == 0
