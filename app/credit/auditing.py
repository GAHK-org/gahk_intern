"""What the audit trail keeps of GAHK-konto: everything. The ledger is its own record and can never
change, so its rows in the trail are a second copy, kept so the audit page shows money like any
other change without code of its own."""

from audit import trail

from .models import (
    Anchor,
    AnchorDelivery,
    Book,
    Closing,
    ClosingBalance,
    CreditAccount,
    Holder,
    Posting,
    Statement,
    Transfer,
)

trail.register(
    Holder,
    "credit.holder",
    name=lambda h: h.name,
    label="Kontohaver",
    labels={"person": "Person", "workgroup": "Embedsgruppe"},
)
trail.register(
    Book,
    "credit.book",
    name=lambda b: b.name,
    label="Kontofører",
    labels={
        "name": "Navn",
        "holder": "Ansvarlige",
        "default_credit_limit_ore": "Kreditgrænse for nye konti (øre)",
        "is_main": "Hovedbog",
        "created_at": "Oprettet",
    },
)
trail.register(
    CreditAccount,
    "credit.account",
    name=str,
    label="GAHK-konto",
    labels={
        "book": "Kontofører",
        "holder": "Kontohaver",
        "name": "Navn",
        "credit_limit_ore": "Kreditgrænse (øre)",
        "bank_reference": "Bankkonto",
        "counterpart_book": "Mellemregning med",
        "created_at": "Oprettet",
        "closed_at": "Lukket",
    },
)
trail.register(
    Transfer,
    "credit.transfer",
    name=lambda t: f"Overførsel #{t.pk}",
    label="Overførsel",
    labels={
        "kind": "Art",
        "created_at": "Tidspunkt",
        "created_by": "Lavet af",
        "source": "Kilde",
        "dedupe_token": "Afsendelses-id",  # nosec B105 - a field label, not a password
        "reverses": "Tilbagefører",
        "companion_of": "Mellemregning for",
    },
)
trail.register(
    Posting,
    "credit.posting",
    name=lambda p: f"{p.account}: {p.amount_ore / 100:.2f} kr".replace(".", ","),
    label="Postering",
    labels={
        "transfer": "Overførsel",
        "account": "Konto",
        "amount_ore": "Beløb (øre)",
        "balance_after": "Saldo efter (øre)",
        "text": "Tekst",
    },
)
trail.register(
    Closing,
    "credit.closing",
    name=str,
    label="Lukket periode",
    labels={
        "book": "Kontofører",
        "period_start": "Fra",
        "period_end": "Til og med",
        "ends_at": "Slut",
        "previous": "Forrige lukning",
        "closed_by": "Lukket af",
        "closed_at": "Lukket",
        "note": "Bemærkning",
        "anchor": "Anker",
    },
)
for _model in (Anchor, AnchorDelivery, ClosingBalance, Statement):
    trail.exempt(_model, "append-only records of their own, made by the jobs and the closing they belong to")
trail.actions(
    {
        "credit.transfer.sent": "Overførte penge",
        "credit.transfer.charged": "Opkrævede",
        "credit.transfer.reversed": "Tilbageførte en overførsel",
        "credit.account.opened": "Oprettede en GAHK-konto",
        "credit.holder.created": "Ny kontohaver",
        "credit.account.limit_changed": "Ændrede en kreditgrænse",
        "credit.account.closed": "Lukkede en GAHK-konto",
        "credit.book.created": "Oprettede en kontofører",
        "credit.book.updated": "Ændrede en kontofører",
        "credit.check.failed": "Kontrol af GAHK-konto fandt en fejl",
        "credit.book.closed": "Lukkede en periode",
    }
)
