"""Forms for the P2 køkkenvagter UI (`docs/plans/2026-09-28-koekkenvagter-p2-design.md`, §2/§3/§6).

Hand-rendered field-by-field in the templates (house convention -- no `label_tag`, see §10), so
these classes only need to define fields/choices/validation, never widget CSS classes for an
auto-rendered form.
"""

from django import forms
from django.db.models import Q

from core.clock import current_date
from residents.models import Resident

# 0 = mandag .. 6 = søndag (Python's date.weekday()), matching PraeferenceDag.weekday.
WEEKDAYS = [
    (0, "Mandag"),
    (1, "Tirsdag"),
    (2, "Onsdag"),
    (3, "Torsdag"),
    (4, "Fredag"),
    (5, "Lørdag"),
    (6, "Søndag"),
]


class PraeferenceForm(forms.Form):
    """The whole preference declaration in one submit -- P2 design doc §2's four inputs. Weekdays
    for the three soft day-preferences are collected as checkboxes, not a single `<select multiple>`
    -- easier to hand-render as a 7-day grid per kind and to keep readable in a template.
    """

    weekday_unavailable = forms.BooleanField(
        required=False,
        label="Jeg er ikke hjemme i dagtimerne på hverdage",
        help_text="Ruter dig til weekend-puljen for morgen-/frokostvagt. Det eneste hårde valg her — "
        "de tre nedenfor er ønsker, ikke garantier.",
    )
    # CheckboxSelectMultiple, not the default <select multiple>: iterated per-subwidget in the
    # template as `{{ checkbox.tag }} {{ checkbox.choice_label }}`, which renders the bare <input>
    # (correct "checked" state included) beside hand-written text -- never Django's own
    # `label_tag`-wrapped `<label>`, per house convention (§10).
    morgen_dage = forms.MultipleChoiceField(
        required=False,
        choices=WEEKDAYS,
        label="Foretrukne dage til morgenvagt",
        widget=forms.CheckboxSelectMultiple,
    )
    frokost_dage = forms.MultipleChoiceField(
        required=False,
        choices=WEEKDAYS,
        label="Foretrukne dage til frokostvagt",
        widget=forms.CheckboxSelectMultiple,
    )
    aften_dage = forms.MultipleChoiceField(
        required=False,
        choices=WEEKDAYS,
        label="Foretrukne dage til aftenvagt",
        widget=forms.CheckboxSelectMultiple,
    )

    def _clean_dage(self, field: str) -> list[int]:
        return sorted(int(d) for d in self.cleaned_data.get(field, []))

    def clean_morgen_dage(self) -> list[int]:
        return self._clean_dage("morgen_dage")

    def clean_frokost_dage(self) -> list[int]:
        return self._clean_dage("frokost_dage")

    def clean_aften_dage(self) -> list[int]:
        return self._clean_dage("aften_dage")


class AnmeldelseForm(forms.Form):
    """ "Denne vagt blev ikke udført" -- P2 design doc §6. `reason` is optional but strongly
    encouraged in the template copy: "an adjudicator with no idea why something was flagged cannot
    do much with it" (design doc)."""

    reason = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Hvorfor melder du denne vagt?"}),
        label="Begrundelse (valgfri)",
    )


class AllokeringForm(forms.Form):
    """Køkkengruppen's month-picker for a manual allocation run -- P2 design doc §7."""

    year = forms.IntegerField(min_value=2020, max_value=2100, label="År")
    month = forms.IntegerField(min_value=1, max_value=12, label="Måned")
    force = forms.BooleanField(
        required=False,
        label="Gentildel (allerede tildelte vagter denne måned)",
        help_text="Kun til en reel rettelse — se Amendment 1, A1.2.",
    )


class OverrideAssignForm(forms.Form):
    """Køkkengruppen's manual override: assign one resident directly to one open `Vagt` slot --
    P2 design doc §7."""

    vagt = forms.IntegerField(label="Vagt (id)", widget=forms.HiddenInput)
    resident = forms.IntegerField(label="Beboer (id)")


class ForeslaaByttForm(forms.Form):
    """ "Foreslå bytte" on an open offer (Amendment 4 step 2): the id of the viewer's own row to offer in
    exchange. Only the shape is checked here; `services.propose_trade` does the real validation."""

    modydelse = forms.IntegerField(min_value=1)


class TilbydForm(forms.Form):
    """ "Tilbyd vagten" (Amendment 4 step 3): whether to also share the offer in Den Hurtige's Køkkenvagter
    channel. Unticked (or absent) posts nothing."""

    del_i_den_hurtige = forms.BooleanField(required=False)


class FravaerForm(forms.Form):
    """An away range (P3 step 2): `fra`/`til` from two hand-rendered `<input type="date">`. Only parses;
    every business rule (order, already ended, summer only, overlap) lives in `services.add_fravaer`."""

    fra = forms.DateField(
        input_formats=["%Y-%m-%d"],
        label="Fra",
        error_messages={"required": "Udfyld fra-datoen.", "invalid": "Fra-datoen er ikke en gyldig dato."},
    )
    til = forms.DateField(
        input_formats=["%Y-%m-%d"],
        label="Til",
        error_messages={"required": "Udfyld til-datoen.", "invalid": "Til-datoen er ikke en gyldig dato."},
    )


class FestKreditForm(forms.Form):
    """Step 1 of an award (Amendment 6): the event, the helpers and a default number of hours. Only
    shape is checked here; `services.festkredit_preview` applies the real rules. The confirmation step
    posts hidden `navn`/`dato` plus one `timer_<resident pk>` field per helper, parsed in the view."""

    navn = forms.CharField(
        max_length=100,
        label="Begivenhed",
        error_messages={"required": "Udfyld navnet på begivenheden."},
    )
    dato = forms.DateField(
        input_formats=["%Y-%m-%d"],
        label="Dato",
        error_messages={"required": "Udfyld datoen.", "invalid": "Datoen er ikke en gyldig dato."},
    )
    hjaelpere = forms.ModelMultipleChoiceField(
        queryset=Resident.objects.none(),
        widget=forms.SelectMultiple,
        label="Hjælpere",
        error_messages={"required": "Vælg mindst én hjælper."},
    )
    timer = forms.IntegerField(
        min_value=1,
        label="Timer pr. person",
        error_messages={"required": "Udfyld antal timer.", "min_value": "Der skal være mindst 1 time."},
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        today = current_date()
        # Only residents who live here now can receive credit (A6 §4); the service re-checks.
        self.fields["hjaelpere"].queryset = Resident.objects.filter(  # type: ignore[attr-defined]
            Q(move_out_date__isnull=True) | Q(move_out_date__gte=today)
        ).order_by("first_name", "last_name", "pk")
