"""Forms for the P2 køkkenvagter UI (`docs/plans/2026-09-28-koekkenvagter-p2-design.md`, §2/§3/§6).

Hand-rendered field-by-field in the templates (house convention -- no `label_tag`, see §10), so
these classes only need to define fields/choices/validation, never widget CSS classes for an
auto-rendered form.
"""

from django import forms

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
