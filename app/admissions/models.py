"""Admission applications — tour (rundvisning) and sublet (fremleje). Legacy `gahk_ansoegninger`.

Clean reimplementation of F-001: explicit fields (no mass-assignment), `gender` as male/female/other
(legacy stored only a `female` bool), a single `submitted_at` instead of day/month/year/timestamp, and a
real `received_by`/`received_at`. 1-year retention is enforced by a scheduled purge (see management cmd).
"""

from django.db import models


class Application(models.Model):
    class Type(models.TextChoices):
        TOUR = "rundvisning", "Rundvisning"
        SUBLET = "fremleje", "Fremleje"

    class Gender(models.TextChoices):
        MALE = "male", "Mand"
        FEMALE = "female", "Kvinde"
        OTHER = "other", "Andet"

    type = models.CharField(max_length=20, choices=Type.choices)
    full_name = models.CharField(max_length=255)
    email = models.EmailField()
    gender = models.CharField(max_length=10, choices=Gender.choices, blank=True)
    age = models.CharField(max_length=50, blank=True)  # legacy free-text; kept faithful
    # tour-only
    study_year = models.CharField(max_length=255, blank=True)
    year_left = models.CharField(max_length=255, blank=True)
    university = models.CharField(max_length=255, blank=True)
    field_of_study = models.CharField(max_length=255, blank=True)
    # sublet-only
    occupation = models.CharField(max_length=255, blank=True)
    # shared
    heard_about_us = models.CharField(max_length=255, blank=True)
    motivation = models.TextField(blank=True)
    submitted_at = models.DateTimeField()
    received_by = models.ForeignKey(
        "residents.Resident",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="applications_received",
    )
    received_at = models.DateTimeField(null=True, blank=True)
    # Indstillingen can discard an application ("Kasseret") — e.g. spam or an obvious non-fit. Discarded
    # applications drop out of the list/search by default but are kept (retention purge still applies)
    # and can be revealed with a toggle, or un-discarded. Nullable, so a plain migration.
    discarded_by = models.ForeignKey(
        "residents.Resident",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="applications_discarded",
    )
    discarded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["type", "submitted_at"]),
            models.Index(fields=["received_by"]),
        ]
        ordering = ["-submitted_at"]

    def __str__(self) -> str:
        return f"{self.get_type_display()}: {self.full_name}"

    @property
    def is_received(self) -> bool:
        return self.received_by_id is not None

    @property
    def is_discarded(self) -> bool:
        return self.discarded_by_id is not None
