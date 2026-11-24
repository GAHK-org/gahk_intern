"""What the audit trail keeps of Ølkælder. Its money is in GAHK-konto, whose every transfer is in the
trail already; Ølkælder's own records join the trail with the rest of the app (it is still among the
pending apps), except the interest exemptions, which say who is spared a charge and so are kept now."""

from audit import trail

from .models import InterestExemption

trail.register(
    InterestExemption,
    "oelkaelder.interest_exemption",
    name=str,
    label="Rentefritagelse",
    labels={
        "credit_account": "Konto",
        "reason": "Begrundelse",
        "starts_on": "Fra",
        "ends_on": "Til og med",
        "created_by": "Oprettet af",
        "created_at": "Oprettet",
    },
)
trail.actions(
    {
        "oelkaelder.exemption.created": "Fritog en ølkælder-konto for rente",
        "oelkaelder.exemption.ended": "Afsluttede en rentefritagelse",
        "oelkaelder.history.imported": "Indlæste Ølkælderens historik i GAHK-konto",
    }
)
