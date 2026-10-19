"""Validation for the untrusted payloads Den Hurtige accepts.

The push-subscription payload moved to core.forms with the rest of the push stack; what is left is
the reaction, whose emoji grammar lives in core.emoji and whose Danish wording lives here.
"""

from django import forms

from core.emoji import is_emoji, is_only_one_emoji, normalize_emoji


class ReactionForm(forms.Form):
    """Validates that the reaction is exactly ONE emoji.

    The grammar lives in core.emoji (flags, keycaps, VS16, skin tones, ZWJ sequences — an emoji is
    not one character). What stays here is the Danish wording, and the distinction between the two
    ways this fails: "that is not an emoji at all" and "that is several emoji", which need different
    messages because the fix differs.
    """

    emoji = forms.CharField(max_length=32)

    def clean_emoji(self) -> str:
        # Functions imported by name, not the module: the field above is also called `emoji`, and
        # `emoji.normalize(...)` in here reads like the field even though it resolves to the module.
        value = normalize_emoji(self.cleaned_data["emoji"])
        if not value:
            raise forms.ValidationError("Vælg en emoji.")
        if not is_only_one_emoji(value):
            raise forms.ValidationError("Kun emoji kan bruges som reaktion.")
        if not is_emoji(value):
            raise forms.ValidationError("Vælg kun én emoji.")
        return value
