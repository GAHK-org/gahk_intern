"""Rooms — two sub-domains:
  * kvotient  : the room-application lottery (F-004) — intern_kvotient_*_nyintern
  * condition : move-in/out condition inspection (F-005) — intern_room_condition / _criteria

Fixes baked in: the K formula is preserved (decided correct); month integers are 0-indexed
(decided); the multi-table application submit is wrapped in a transaction at the view layer; the
cross-month cascade delete on closeOffer is scoped to the offer's month; the legacy delimited
criteria/comment/image blobs are **normalized** into RoomConditionScore rows; only the current
condition per room is kept; images are stored as files (FileField) with paths in the DB.
"""

from django.db import models
from django.utils import timezone


# ----------------------------------------------------------------------------- kvotient (F-004)
class KvotientApplication(models.Model):  # intern_kvotient_nyintern
    resident = models.ForeignKey(
        "residents.Resident", on_delete=models.CASCADE, related_name="kvotient_applications"
    )
    move_month = models.IntegerField()  # 0-indexed month number (decided)
    move_in_month = models.IntegerField()  # 0-indexed
    done_studying_month = models.IntegerField()
    k = models.FloatField()  # ranking quotient; computed at submit (formula kept)
    apply_datetime = models.DateTimeField(default=timezone.now)  # a record; NOT an allocation tiebreak

    class Meta:
        ordering = ["-k", "apply_datetime"]
        # One application per resident per round. A "round" is a move_month; between rounds end_round /
        # close_offer wipe the applications, so this never blocks a legitimate re-application later.
        constraints = [
            models.UniqueConstraint(fields=["resident", "move_month"], name="uniq_resident_move_month")
        ]

    def __str__(self) -> str:
        return f"Kvotient {self.resident.full_name} (K={self.k:.2f})"


class KvotientPriority(models.Model):  # intern_kvotient_priority_nyintern
    application = models.ForeignKey(KvotientApplication, on_delete=models.CASCADE, related_name="priorities")
    room = models.ForeignKey("core.Room", on_delete=models.PROTECT, related_name="+")
    priority = models.PositiveSmallIntegerField()  # 1 = first choice
    month = models.IntegerField(null=True, blank=True)  # legacy column (usually unset); kept for fidelity

    class Meta:
        ordering = ["priority"]
        constraints = [models.UniqueConstraint(fields=["application", "priority"], name="uniq_app_priority")]


class KvotientOrlov(models.Model):  # intern_kvotient_orlov_nyintern (leave-of-absence periods)
    application = models.ForeignKey(
        KvotientApplication, on_delete=models.CASCADE, related_name="orlov_periods"
    )
    start_month = models.IntegerField()
    end_month = models.IntegerField()

    @property
    def number_of_months(self) -> int:
        return self.end_month - self.start_month


class RoomOffer(models.Model):  # intern_kvotient_offer_nyintern (a room offered in a given month)
    room = models.ForeignKey("core.Room", on_delete=models.PROTECT, related_name="offers")
    month = models.IntegerField()  # the month this room is offered for
    # Manual tie resolution (F-004): when equal-K applicants contest this room, indstilling picks the
    # winner (coin flip in the dorm) and it is recorded here; allocate_round then treats the room as
    # decided. Ephemeral — the offer (and this pointer) is deleted when the round ends.
    awarded_application = models.ForeignKey(
        "KvotientApplication", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [models.UniqueConstraint(fields=["room", "month"], name="uniq_room_offer_month")]

    def __str__(self) -> str:
        return f"Tilbud: {self.room} ({self.month})"


# --------------------------------------------------------------------------- condition (F-005)
class RoomCriterion(models.Model):  # intern_room_criteria
    code = models.CharField(max_length=50, unique=True)  # legacy varchar id
    name = models.CharField(max_length=50)
    description = models.TextField(blank=True)  # the score legend shown under the field (F-005)
    options = models.PositiveSmallIntegerField()  # scale *shape* selector — see score_values

    def __str__(self) -> str:
        return self.name

    @property
    def score_values(self) -> list[int]:
        """The scores this criterion accepts, mirroring legacy besvar.php:27-40 exactly:

            options == 3  -> 0, 1, 2
            options  > 2  -> 1 .. options        (options == 5 -> 1..5)
            otherwise     -> 0, 1

        `options` is a *shape* selector, not a maximum — only the ==3 case is zero-based. Of the 28
        real criteria, 13 use 5 (->1..5), 9 use 3 (->0..2) and 7 use 2 (->0..1). Always contiguous,
        so score_min/score_max describe the scale fully.
        """
        if self.options == 3:
            return [0, 1, 2]
        if self.options > 2:
            return list(range(1, self.options + 1))
        return [0, 1]

    @property
    def score_min(self) -> int:
        return self.score_values[0]

    @property
    def score_max(self) -> int:
        return self.score_values[-1]

    def accepts_score(self, value: int | None) -> bool:
        """None (unanswered) is always acceptable; anything else must be on the scale."""
        return value is None or value in self.score_values


class RoomCondition(models.Model):  # intern_room_condition (only the current state kept, decided)
    room = models.ForeignKey("core.Room", on_delete=models.CASCADE, related_name="conditions")
    resident = models.ForeignKey(
        "residents.Resident", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    recorded_by_name = models.CharField(max_length=255, blank=True)  # legacy alumne_fullname snapshot
    recorded_at = models.DateTimeField()
    is_current = models.BooleanField(default=True)

    class Meta:
        indexes = [models.Index(fields=["room", "is_current"])]

    def __str__(self) -> str:
        return f"Tilstand {self.room} @ {self.recorded_at:%Y-%m-%d}"


class RoomConditionScore(models.Model):
    """Normalized from the legacy delimited criteria/comment/image blobs (F-005)."""

    condition = models.ForeignKey(RoomCondition, on_delete=models.CASCADE, related_name="scores")
    criterion = models.ForeignKey(RoomCriterion, on_delete=models.PROTECT, related_name="+")
    score = models.IntegerField(null=True, blank=True)
    comment = models.TextField(blank=True)
    # Legacy image path(s) as a raw string, kept read-only from the migration.
    image = models.TextField(blank=True)
    # New uploads (F-005 refinement): a real file under MEDIA_ROOT. FileField → no Pillow dependency.
    photo = models.FileField(upload_to="roomimages/%Y/", max_length=255, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["condition", "criterion"], name="uniq_condition_criterion")
        ]

    @property
    def image_urls(self) -> list[str]:
        """Served URLs for the migrated legacy image(s). The legacy `image` field is a ';'-separated
        list of paths (mixed `public/` and `/public/` prefixes); each becomes a MEDIA_ROOT URL (or is
        passed through if already absolute). New uploads use `photo.url` instead."""
        from django.conf import settings

        base = settings.MEDIA_URL.rstrip("/")
        urls = []
        for part in (self.image or "").split(";"):
            v = part.strip()
            if not v:
                continue
            urls.append(v if v.startswith(("http://", "https://")) else f"{base}/{v.lstrip('/')}")
        return urls
