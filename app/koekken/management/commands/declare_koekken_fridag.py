"""Declare a fridag -- Amendment 5 (A5.4): a date (or a chosen set of shift kinds on it) the kitchen
does not need covering, e.g. juleaften. The normal case, not an edge case: a periode is allocated
~122 days before it starts, so a fridag declared in the months leading up to it is almost always
against an already-generated/already-allocated month -- this ONE action creates the Fridag row(s),
deletes the matching Vagt row(s) if that month was already generated, re-allocates the affected month
(the existing --force mechanism) and re-posts its obligation -- the two re-runs gated by two SEPARATE,
independent checks (2026-10 review, F3, refined by finding (b); see koekken.services.declare_fridag):
re-allocation only when the month already had `TILDELT` rows to reseat, re-posting only when the
month already had obligation posted for it -- NEITHER tied to the other, since a month can be fully
settled (nothing left to reseat) while still needing its obligation reconciled down. All in one atomic
step, then notifies any resident who genuinely lost an assignment as a net result (F2).
See koekken.services.declare_fridag for the full mechanics and its two guards (no UDFOERT/ANMELDT
assignment may be disturbed, no date in the past).

**Notification dispatch happens HERE, not inside the service function, and only when `not dry_run`**
(2026-10 review, F1): `declare_fridag` returns ready-to-send `(resident, audience, message)` tuples in
`FridagResult.notifications` instead of sending them itself, specifically so that a `--dry-run` --
which rolls its wrapping transaction back via `transaction.set_rollback(True)` AFTER the service
function has already returned -- can never have a real push already in flight by the time that
rollback happens. Sent inline (`background=False`), matching `events.services.
send_deadline_reminder`'s reasoning for anything reachable from a management command: a daemon thread
started from `handle()` is killed mid-send the moment the process exits.

No UI view exists for this yet (P2 design doc's "Phasing" -- this amendment adds no new UI surface);
--dry-run is this action's preview, matching every other koekken management command's own --dry-run
convention: run it first to see exactly what would change (how many Fridag rows, how many Vagt rows
deleted, which month(s) re-allocated and re-posted, and -- F5 -- which residents are actually affected,
either notified of a cancelled shift or left with a merely-moved one) before committing for real.

**Recommended timing, per the stakeholder:** declare a periode's fridage BEFORE generating that
periode's shifts (i.e. before `generate_koekkenvagter` runs for it), rather than after generation or
in the middle of an already-allocated periode. Declared this early, there is nothing yet to delete,
re-allocate or re-post; `generate_vagter`'s own `is_fridag` seam (A5.3) simply never creates those
rows in the first place, which is the simpler of this command's two paths. (2026-10 review round 2,
F5: this note previously said "before its deadline-triggered batch allocation runs", which is a wider
window that includes AFTER generation but before allocation -- that window does have `Vagt` rows to
delete, exactly what `test_declare_fridag_on_not_yet_allocated_month_only_deletes_vagt_rows` exercises,
contradicting the "nothing yet to delete" claim.)"""

import argparse
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.push import send
from koekken.models import VagtRegel
from koekken.services import FridagResult, KoekkenAllocationError, declare_fridag


class Command(BaseCommand):
    help = (
        "Erklær en fridag for en dato (eller udvalgte vagttyper på den) -- opret Fridag-række(r), "
        "slet evt. allerede genererede vagter, genallokér og genbogfør de(n) berørte måned(er) hvis "
        "den allerede var allokeret, og notificér beboere der reelt mister en vagt. --dry-run viser "
        "konsekvensen -- inklusive hvem der berøres -- uden at gennemføre den eller sende noget. "
        "Anbefales erklæret som en del af generering af periodens vagter, dvs. før periodens "
        "deadline-udløste batch-allokering kører -- så er der intet at genallokere eller genbogføre."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("date", type=str, help="YYYY-MM-DD")
        parser.add_argument(
            "--kind",
            action="append",
            dest="kinds",
            choices=[k.value for k in VagtRegel.Kind],
            default=None,
            help="Gentag for flere vagttyper. Udelades: hele dagen (alle vagttyper).",
        )
        parser.add_argument("--reason", type=str, default="", help="F.eks. 'Juleaften'.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: object, **opts: object) -> None:
        for_date = date.fromisoformat(str(opts["date"]))
        kinds_opt = opts["kinds"]
        kinds: list[str] = (
            list(kinds_opt) if isinstance(kinds_opt, list) else [k.value for k in VagtRegel.Kind]
        )
        reason = str(opts["reason"])
        dry_run = bool(opts["dry_run"])

        try:
            with transaction.atomic():
                result = declare_fridag(for_date, kinds, reason)
                if dry_run:
                    transaction.set_rollback(True)
        except KoekkenAllocationError as exc:
            raise CommandError(str(exc)) from exc

        # F1: dispatched only now that the transaction above is known to have actually committed --
        # never under --dry-run, which rolled it back instead.
        if not dry_run:
            for _resident, audience, message in result.notifications:
                send(audience, "Køkkenvagt", message, "/intern/koekken/", background=False)

        self._report(for_date, dry_run, result)

    def _report(self, for_date: date, dry_run: bool, result: FridagResult) -> None:
        """F5: an actual preview, not bare counts -- names the affected month(s) and the residents
        whose assignment changed, on both --dry-run (so it is a useful preview) and a real run (as a
        record of what happened).

        2026-10 review round 2, F2: reallocation and obligation re-posting are reported from their
        OWN, independently-gated fields (`result.reallocated_months` /
        `result.obligation_reposted_months` -- see `FridagResult`'s docstring) on SEPARATE lines,
        never folded into one combined "genallokeret og genbogført" claim the way the previous
        version did. That combined claim was only ever true of `reallocated_months`, so it was wrong
        whenever obligation was re-posted WITHOUT a reallocation (a month fully settled past `TILDELT`
        still needing its obligation reconciled down) -- it both under-reported (missing the actual
        repost) and, via its "ingen (måneden var endnu ikke allokeret, eller intet blev slettet)"
        fallback, actively asserted that nothing was deleted and nothing re-posted even when a `Vagt`
        row WAS deleted and obligation WAS re-posted. Reporting each field on its own line, naming
        only what that field actually says happened, can never produce that contradiction."""
        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}{for_date}: {len(result.created)} fridag-række(r) oprettet, "
                f"{result.deleted_vagter} vagt(er) slettet."
            )
        )
        if result.reallocated_months:
            months = ", ".join(f"{y}-{m:02d}" for y, m in result.reallocated_months)
            self.stdout.write(f"  {len(result.reallocated_months)} måned(er) genallokeret ({months}).")
        else:
            self.stdout.write("  Ingen måneder genallokeret.")
        if result.obligation_reposted_months:
            months = ", ".join(f"{y}-{m:02d}" for y, m in result.obligation_reposted_months)
            self.stdout.write(f"  {len(result.obligation_reposted_months)} måned(er) genbogført ({months}).")
        else:
            self.stdout.write("  Ingen måneder genbogført.")
        if result.notifications:
            names = ", ".join(resident.full_name for resident, _audience, _message in result.notifications)
            self.stdout.write(
                f"  {len(result.notifications)} beboer(e) mistede en vagt og "
                f"{'ville være' if dry_run else 'er'} blevet notificeret: {names}."
            )
        if result.moved_residents:
            names = ", ".join(r.full_name for r in result.moved_residents)
            self.stdout.write(
                f"  {len(result.moved_residents)} beboer(e) fik en vagt flyttet (ikke aflyst, ikke "
                f"notificeret): {names}."
            )
