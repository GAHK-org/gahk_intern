"""Validation for the untrusted push-subscription payload.

The body is `frontend/src/push.ts`'s serialisation of the browser's own PushSubscription:

    {"status_type": "subscribe",
     "subscription": {"endpoint": "...", "keys": {"auth": "...", "p256dh": "..."}},
     "user_agent": "..."}

It is untrusted input on a login-gated endpoint, so every field is length-checked before it reaches
the database — the columns are narrow and a crafted endpoint would otherwise raise DataError.

A `topic` field from a cached copy of the old per-feature push.ts is ignored rather than rejected:
a device now subscribes once for everything, which is what that build was asking for anyway.
"""

from typing import Any

from django import forms


class PushSubscriptionForm(forms.Form):
    status_type = forms.ChoiceField(
        choices=[("subscribe", "subscribe"), ("unsubscribe", "unsubscribe"), ("status", "status")]
    )
    # assume_scheme silences Django 6.0's URLField deprecation; push endpoints are always https.
    endpoint = forms.URLField(max_length=500, assume_scheme="https")
    # Not required for `status` / `unsubscribe`, which only need the endpoint; subscribe checks them.
    auth = forms.CharField(max_length=100, required=False)
    p256dh = forms.CharField(max_length=100, required=False)
    user_agent = forms.CharField(max_length=500, required=False)

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "PushSubscriptionForm":
        """Flatten the nested browser payload into this form's flat fields. Missing or wrongly typed
        branches collapse to empty dicts so the form reports them as validation errors rather than
        raising AttributeError on malformed input."""
        subscription = data.get("subscription")
        subscription = subscription if isinstance(subscription, dict) else {}
        keys = subscription.get("keys")
        keys = keys if isinstance(keys, dict) else {}
        return cls(
            {
                "status_type": data.get("status_type"),
                "endpoint": subscription.get("endpoint"),
                "auth": keys.get("auth"),
                "p256dh": keys.get("p256dh"),
                "user_agent": data.get("user_agent") or "",
            }
        )

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean() or {}
        if cleaned.get("status_type") == "subscribe" and not (cleaned.get("auth") and cleaned.get("p256dh")):
            raise forms.ValidationError("A subscription needs its encryption keys.")
        return cleaned
