from django import forms

from .models import DocumentType


class DocumentCreateForm(forms.Form):
    title = forms.CharField(max_length=200, label="Titel")
    document_type = forms.ChoiceField(choices=DocumentType.choices, label="Type")


class DocumentGrantForm(forms.Form):
    email = forms.EmailField(label="Beboerens e-mail")
    permission = forms.ChoiceField(choices=(("editor", "Kan redigere"), ("viewer", "Kan læse")))
