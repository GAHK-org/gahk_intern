"""Forms for opslagstavlen.

Real ModelForms here, unlike Den Hurtige's hand-rolled `request.POST` reading. That feature has one
textarea and a duration whitelist; this one has a category that must be validated against a choice
set, a long body with a length cap, and an edit view that has to round-trip the same fields —
which is exactly what the project's form convention (explicit `Meta.fields`, Danish labels, manual
field-by-field rendering in the template) exists for.
"""

from django import forms

from access import audiences
from core.emoji import is_emoji, is_only_one_emoji, normalize_emoji

from .models import MAX_BODY_CHARS, Notice, NoticeComment


def linkable_events() -> object:
    """The events a post may point at: upcoming, not cancelled, and OPEN TO EVERY RESIDENT.

    The visibility filter is the load-bearing one. Opslagstavlen is readable by the whole house, so
    a chip reading "Fødselsdag i 104 · 5. september" on a post about a private party would announce
    the party to sixty people who were not invited — the one thing `kun inviterede` exists to
    prevent. Restricting the CHOICES is also what validates the POST; see NoticeForm.__init__.

    Cancelled events are dropped because linking to one is never what somebody means to do. An event
    cancelled *after* it was linked keeps its chip, which is right — the post is how people find out
    it is off.

    Imported inside the function, not at module scope: `events` is a younger app that already points
    back at this one in prose, and a deferred import keeps the dependency one-directional at import
    time as well as on paper.
    """
    from events.models import Event, Visibility

    return Event.objects.upcoming().filter(visibility=Visibility.AABENT, cancelled_at__isnull=True)


class NoticeForm(forms.ModelForm):
    audience_preset = forms.ChoiceField(label="Hvem kan se det", choices=())

    class Meta:
        model = Notice
        # Explicit, which kills mass-assignment: pinned_at/pinned_by/author/edited_at are set by the
        # views that are allowed to set them, never by whatever arrives in the POST body.
        fields = ["category", "body", "event"]
        labels = {"category": "Kategori", "body": "Indhold", "event": "Handler om"}
        help_texts = {
            "event": "Valgfrit. Knytter opslaget til en begivenhed, så folk kan gå direkte til tilmeldingen.",
        }
        widgets = {
            "body": forms.Textarea(
                attrs={
                    "rows": 16,
                    "maxlength": MAX_BODY_CHARS,
                    "placeholder": "Skriv med Markdown. Brug knapperne ovenfor, eller skriv **fed**.",
                    "data-mentionable": "",
                }
            ),
        }

    def __init__(
        self,
        *args: object,
        events_allowed: bool = True,
        presets: "tuple[audiences.Preset, ...]" = audiences.PRESETS,
        **kwargs: object,
    ) -> None:
        """Bind the event choices HERE, never at class level.

        Two reasons, and the first is the ordinary one: the queryset depends on "now", so a
        class-level definition would be evaluated at import and offer whichever events were upcoming
        when the process started.

        The second is the security one. The choices are the ONLY events a POST can name — a
        ModelChoiceField validates the submitted id against this queryset — so restricting it here
        is also what stops somebody posting the id of a private event they were not invited to and
        having its title rendered on a board the whole house reads.

        `events_allowed=False` REMOVES the field rather than disabling it. Begivenheder has its own
        rollout gate, and offering somebody a list of events they cannot open is at best confusing;
        removing it also means a POST naming one is ignored rather than merely unrendered, which is
        the difference between a hidden control and an absent one.
        """
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        # Who reads it: one of the presets the author may pick (access.audiences). An audience saved
        # that is no longer offered stays, until the author picks another.
        self._presets = {preset.key: preset for preset in presets}
        current = audiences.preset_of(self.instance.audience) if self.instance.pk else presets[0]
        choices = [(preset.key, preset.label) for preset in presets]
        if current is None or current.key not in self._presets:
            choices.insert(0, ("", audiences.label(self.instance.audience)))
        audience = self.fields["audience_preset"]
        audience.choices = choices  # type: ignore[attr-defined]
        audience.initial = current.key if current is not None and current.key in self._presets else ""
        audience.required = False
        if not events_allowed:
            del self.fields["event"]
            return
        self.fields["event"].queryset = linkable_events()  # type: ignore[attr-defined]
        self.fields["event"].empty_label = "Ingen"  # type: ignore[attr-defined]

    def clean_audience_preset(self) -> str:
        key = self.cleaned_data.get("audience_preset") or ""
        if key and key not in self._presets:
            raise forms.ValidationError("Vælg en af mulighederne.")
        return key

    def save(self, commit: bool = True) -> Notice:
        notice = super().save(commit=False)
        key = self.cleaned_data.get("audience_preset")
        if key:
            notice.audience = sorted(self._presets[key].roles)
        if commit:
            notice.save()
            self.save_m2m()
        return notice

    def clean_body(self) -> str:
        """Cap the body server-side as well as via maxlength.

        Not tidiness: the list page renders every post's Markdown, so an enormous body would slow
        down (or break) the whole board rather than one post — a crafted POST must not be able to do
        that.
        """
        body = (self.cleaned_data.get("body") or "").strip()
        if not body:
            raise forms.ValidationError("Skriv et indhold.")
        if len(body) > MAX_BODY_CHARS:
            raise forms.ValidationError(f"Opslaget må højst fylde {MAX_BODY_CHARS} tegn.")
        return body


class NoticeCommentForm(forms.ModelForm):
    class Meta:
        model = NoticeComment
        fields = ["body"]
        labels = {"body": "Kommentar"}
        widgets = {
            "body": forms.Textarea(
                attrs={"rows": 3, "placeholder": "Skriv en kommentar…", "data-mentionable": ""}
            ),
        }

    def clean_body(self) -> str:
        """Strips, and no longer REJECTS an empty body.

        A comment may be a photo on its own, and this form never sees the photo: it is validated
        outside, against core.uploads, so that a refused file can warn and be dropped rather than
        failing the whole submission (the same split Den Hurtige uses). "Neither text nor picture"
        is therefore a question only the view can answer, and it does.
        """
        return (self.cleaned_data.get("body") or "").strip()


class ReactionForm(forms.Form):
    """Exactly one emoji. The grammar is shared with Den Hurtige (core.emoji); what lives here is the
    Danish wording, and the split between "not an emoji at all" and "several emoji", which need
    different messages because the fix differs."""

    emoji = forms.CharField(max_length=32)

    def clean_emoji(self) -> str:
        # Imported by name rather than as a module: the field above is also called `emoji`, and
        # `emoji.normalize_emoji(...)` in here would read like the field even though it resolves to
        # the module.
        value = normalize_emoji(self.cleaned_data["emoji"])
        if not value:
            raise forms.ValidationError("Vælg en emoji.")
        if not is_only_one_emoji(value):
            raise forms.ValidationError("Kun emoji kan bruges som reaktion.")
        if not is_emoji(value):
            raise forms.ValidationError("Vælg kun én emoji.")
        return value
