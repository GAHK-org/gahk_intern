import io
import string

from django import forms
from django.core.files.uploadedfile import UploadedFile
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from residents.models import Resident

from .models import AlumneregningSettings


class EconomicAccountNumberForm(forms.ModelForm):
    """Regnskab sets or clears a resident's e-conomic chart-of-accounts accountNumber (alumneregning
    automation, F-010 regnskab role)."""

    class Meta:
        model = Resident
        fields = ["economic_account_number"]
        labels = {"economic_account_number": "E-conomic kontonr."}


class AlumneregningSettingsForm(forms.ModelForm):
    """Regnskab edits the alumneregning mail body and følgebrev PDF in the UI (F-010)."""

    class Meta:
        model = AlumneregningSettings
        fields = ["message_template", "followup_pdf"]
        labels = {
            "message_template": "Besked",
            "followup_pdf": "Følgebrev (PDF)",
        }
        widgets = {
            "message_template": forms.Textarea(attrs={"rows": 16}),
            "followup_pdf": forms.ClearableFileInput(
                attrs={"accept": "application/pdf", "style": "display:none"}
            ),
        }
        help_texts = {
            "message_template": (
                "Pladsholdere, der udskiftes automatisk: $FORNAVN, $SALDO_LINE "
                "(sætningen om gæld/tilgodehavende), $REG_NR, $KONTO_NR."
            ),
            "followup_pdf": (
                "Valgfrit følgebrev, sættes ind som de(n) første side(r) i selve regnings-PDF'en til "
                "hver alumne — ikke en separat vedhæftning. Lad stå tom for ingen følgebrev. Opdateres "
                'typisk ca. hvert halve år. "How to aflæse din alumneregning" står altid med bagerst i '
                "PDF'en, uanset om der er et følgebrev."
            ),
        }

    def clean_followup_pdf(self) -> object:
        upload = self.cleaned_data.get("followup_pdf")
        if isinstance(upload, UploadedFile):
            name = (upload.name or "").lower()
            if upload.content_type != "application/pdf" and not name.endswith(".pdf"):
                raise forms.ValidationError("Følgebrevet skal være en PDF-fil.")
            # Extension/content-type alone don't prove it's a real PDF — a bad upload here would
            # otherwise only surface later, as a crash of every alumneregning send that reads it.
            try:
                len(PdfReader(io.BytesIO(upload.read())).pages)
            except PyPdfError as exc:
                raise forms.ValidationError("Filen kunne ikke læses som en PDF.") from exc
            finally:
                upload.seek(0)
        return upload

    def clean_message_template(self) -> str:
        # safe_substitute() (regnskab.alumneregning._body) leaves an unknown placeholder as literal
        # text instead of raising — exactly what you want for a typo in a template nobody's about to
        # retype under pressure, but it means a typo like $FORNAVNN would otherwise go out to every
        # alumne unexpanded. Catch it here, at the one point a human reviews the wording.
        template = self.cleaned_data["message_template"]
        allowed = {"FORNAVN", "SALDO_LINE", "REG_NR", "KONTO_NR"}
        unknown = set(string.Template(template).get_identifiers()) - allowed
        if unknown:
            raise forms.ValidationError(
                f"Ukendt(e) pladsholder(e): {', '.join(sorted('$' + u for u in unknown))}. "
                f"Tilladte: $FORNAVN, $SALDO_LINE, $REG_NR, $KONTO_NR."
            )
        return template
