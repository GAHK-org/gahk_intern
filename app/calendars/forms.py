from django import forms

from .models import CalendarSubscription


class SubscriptionForm(forms.ModelForm):
    class Meta:
        model = CalendarSubscription
        fields = ("events", "include_birthdays")
        widgets = {"events": forms.RadioSelect}
        labels = {"events": "Begivenheder", "include_birthdays": "Fødselsdage"}
