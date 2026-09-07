"""The create/edit form, including the invite picker.

The visibility toggle and the picker ship together on purpose: a private event with no invitees is
one nobody — not even its organiser — can find in a list, so a mode where the toggle exists and the
picker does not would be reachable and broken.

THE MINIMUM IS ENFORCED HERE, and it has to be. "At least four invitees besides the organiser" is a
condition across related rows, which no CheckConstraint can express without a trigger. That makes it
the form's job on create AND on edit — the create-only version is the obvious bug, and it has its
own test.
"""

import datetime

from django import forms
from django.conf import settings

from core.clock import current_datetime
from core.uploads import check_image_upload
from residents.models import Residency, Resident, active_period

from .models import MIN_INVITEES, Event, EventComment, Visibility


def invitable_residents(exclude: Resident | None = None) -> object:
    """Who can be invited: whoever lives here this month, minus the organiser.

    Current residency rather than "every Resident row", because the table also holds alumni — a
    picker offering four hundred names, most of whom moved out years ago, is not a picker. This is
    the same question ak.services asks, and it is answered the same way.

    The organiser is dropped because they are implicitly part of their own event; leaving them in
    would let somebody satisfy the four-person minimum by inviting themselves.
    """
    year, month = active_period()
    living_here = Residency.objects.filter(year=year, month=month).values("resident_id")
    qs = Resident.objects.filter(pk__in=living_here).order_by("first_name", "last_name")
    return qs.exclude(pk=exclude.pk) if exclude else qs


class LocalDateTimeInput(forms.DateTimeInput):
    """A native datetime-local picker.

    `type=datetime-local` rather than a text field with a JS calendar: every phone and desktop
    browser already has one, it is localised for free, and on iOS it gives the wheel picker — the
    same argument Den Hurtige's composer makes for keeping its duration a native <select>.

    The format matters. `datetime-local` only pre-fills a value shaped exactly "YYYY-MM-DDTHH:MM";
    Django's default rendering has a space instead of the T, which the browser silently discards, so
    an edit form would come up blank and quietly wipe the time on save.
    """

    input_type = "datetime-local"

    def __init__(self, **kwargs: object) -> None:
        super().__init__(format="%Y-%m-%dT%H:%M", **kwargs)  # type: ignore[arg-type]


class ResidentChoiceField(forms.ModelMultipleChoiceField):
    """Names only in the guest list.

    Resident.__str__ is "Ann Anden <ann@gahk.dk>", which is right in the admin and wrong here:
    sixty rows of address noise in a list you are meant to scan by name, and everybody's e-mail on
    screen for no reason. The search box filters on the rendered label, so shortening it also stops
    a search for "ak" matching half the house through their addresses.
    """

    def label_from_instance(self, obj: Resident) -> str:
        return obj.full_name


class EventForm(forms.ModelForm):
    """`organiser` is not a field — the view sets it. `invitees` is not a model field either: the
    invites are their own table, reconciled in `save_invites` below."""

    invitees = ResidentChoiceField(
        queryset=Resident.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Inviterede",
    )

    class Meta:
        model = Event
        fields = [
            "title",
            "description",
            "image",
            "location",
            "starts_at",
            "ends_at",
            "visibility",
            "capacity",
            "rsvp_deadline_at",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 6}),
            "starts_at": LocalDateTimeInput(),
            "ends_at": LocalDateTimeInput(),
            "rsvp_deadline_at": LocalDateTimeInput(),
            # Radios, not a <select>: two options that change what the rest of the form means are
            # worth showing both of at once, and a phone renders a two-item select as a wheel.
            "visibility": forms.RadioSelect,
        }
        labels = {
            "visibility": "Hvem kan se den?",
            "title": "Titel",
            "description": "Beskrivelse",
            "image": "Billede",
            "location": "Sted",
            "starts_at": "Starter",
            "ends_at": "Slutter",
            "capacity": "Antal pladser",
            "rsvp_deadline_at": "Svarfrist",
        }
        help_texts = {
            "description": "Markdown virker — ligesom i Ankebogen.",
            "capacity": "Lad stå tomt hvis der ikke er nogen grænse. "
            "Når der er fuldt, kommer flere på venteliste.",
            # No entry for the three datetime fields: they are declared below, and a declared field
            # ignores Meta.help_texts entirely.
        }

    # Accepts the value the browser's datetime-local actually sends (with a T) as well as the space
    # form, so a hand-typed or pasted value is not rejected for a separator nobody can see.
    starts_at = forms.DateTimeField(
        label="Starter",
        widget=LocalDateTimeInput(),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"],
    )
    ends_at = forms.DateTimeField(
        label="Slutter",
        required=False,
        widget=LocalDateTimeInput(),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"],
    )
    rsvp_deadline_at = forms.DateTimeField(
        label="Svarfrist",
        required=False,
        widget=LocalDateTimeInput(),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"],
        # Declared here rather than in Meta.help_texts, which a DECLARED field ignores — the three
        # datetime fields are declared for their input_formats, so their labels and help text have
        # to come with them or they silently vanish from the page.
        #
        # It says what the empty value MEANS, because "ingen frist" would otherwise read as "svar
        # hvornår du vil" and the rule is the opposite. See services.answers_locked.
        help_text=(
            "Lad stå tomt hvis der ikke er nogen frist — så lukker svarene når begivenheden "
            "starter. Bagefter kan ingen ændre deres svar."
        ),
    )

    def __init__(self, *args: object, organiser: Resident | None = None, **kwargs: object) -> None:
        """`organiser` is passed by the view so they can be kept out of their own guest list.

        The queryset is bound HERE rather than at class definition, because it depends on
        active_period() — a class-level queryset would be evaluated at import and freeze whichever
        month the process happened to start in.
        """
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        who = organiser or (self.instance.organiser if self.instance.pk else None)
        self.fields["invitees"].queryset = invitable_residents(exclude=who)  # type: ignore[attr-defined]
        if self.instance.pk:
            self.fields["invitees"].initial = [i.resident_id for i in self.instance.invites.all()]

    def clean(self) -> dict:
        """The four-invitee minimum, on create and on edit alike.

        Not a CheckConstraint: it counts rows in another table, which SQL cannot express as a check
        without a trigger. Runs on every save, so switching an existing open event to invite-only
        has to satisfy it too — that transition is the one a create-only check would miss.
        """
        cleaned = super().clean() or self.cleaned_data
        if cleaned.get("visibility") == Visibility.KUN_INVITEREDE:
            chosen = cleaned.get("invitees") or []
            if len(chosen) < MIN_INVITEES:
                self.add_error(
                    "invitees",
                    f"Vælg mindst {MIN_INVITEES} beboere ud over dig selv, "
                    "eller gør begivenheden åben for alle.",
                )
        return cleaned

    def clean_image(self) -> object:
        """One image policy for the whole site, in Danish. See core.uploads."""
        upload = self.cleaned_data.get("image")
        if upload and hasattr(upload, "content_type"):
            problem = check_image_upload(upload, settings.EVENT_IMAGE_MAX_MB)
            if problem:
                raise forms.ValidationError(problem)
        return upload

    def clean_ends_at(self) -> datetime.datetime | None:
        end = self.cleaned_data.get("ends_at")
        start = self.cleaned_data.get("starts_at")
        if end and start and end <= start:
            raise forms.ValidationError("Sluttidspunktet skal ligge efter starten.")
        return end

    def clean_rsvp_deadline_at(self) -> datetime.datetime | None:
        """A deadline after the start is refused rather than quietly ignored.

        `answers_locked` closes at the start regardless, so such a deadline has no effect at all —
        and a field that displays "svar inden lørdag" while the answers actually shut on Friday is
        worse than no field. Refusing is the only version that cannot mislead.
        """
        deadline = self.cleaned_data.get("rsvp_deadline_at")
        start = self.cleaned_data.get("starts_at")
        if deadline and start and deadline > start:
            raise forms.ValidationError(
                "Svarfristen skal ligge før begivenheden starter — svarene lukker under alle "
                "omstændigheder ved start."
            )
        return deadline

    def clean_starts_at(self) -> datetime.datetime:
        start: datetime.datetime = self.cleaned_data["starts_at"]
        # Only on CREATE. An existing event may legitimately be edited while it is running, or just
        # after — fixing the location of tonight's dinner at 18.05 must not be refused because the
        # start is now in the past.
        if self.instance.pk is None and start < current_datetime():
            raise forms.ValidationError("Begivenheden kan ikke starte i fortiden.")
        return start

    def clean_capacity(self) -> int | None:
        capacity = self.cleaned_data.get("capacity")
        if capacity is None or self.instance.pk is None:
            return capacity
        # Refusing rather than silently demoting: somebody was told they were coming. The organiser
        # gets a number they can act on instead of a quiet betrayal of four people.
        #
        # Seating is derived (services.seated), so LOWERING the cap would otherwise just move the
        # tail of the list onto the venteliste with no warning and no notification — which is
        # exactly the silent demotion this refuses.
        from . import services

        taken = len(services.seated(self.instance))
        if capacity < taken:
            raise forms.ValidationError(
                f"Der er allerede {taken} tilmeldte. Antallet kan ikke sættes under {taken}."
            )
        return capacity


class EventCommentForm(forms.ModelForm):
    """One comment. Mirrors reparationer.RepairCommentForm, including the strip-then-reject: a
    textarea full of spaces is `is_valid()` to Django and an empty bubble to a reader."""

    class Meta:
        model = EventComment
        fields = ["body"]
        labels = {"body": "Kommentar"}
        widgets = {
            "body": forms.Textarea(attrs={"rows": 3, "placeholder": "Skriv en kommentar…"}),
        }

    def clean_body(self) -> str:
        body = (self.cleaned_data.get("body") or "").strip()
        if not body:
            raise forms.ValidationError("Skriv en kommentar.")
        return body
