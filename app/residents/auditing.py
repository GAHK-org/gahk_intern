"""What the audit trail keeps of residents, their months and roles, and of logging in."""

from audit import trail

from .models import AdminAccessGrant, Person, Residency, Resident

trail.register(
    Resident,
    "residents.resident",
    name=lambda r: r.full_name or r.email,
    label="Beboer",
    # last_login moves at every login, which is its own event; is_staff follows the roles and the
    # thumbnail follows the picture, both kept by code rather than by anyone's choice.
    ignore={"last_login", "is_staff", "profile_thumbnail", "groups", "user_permissions"},
    redact={"password"},
    labels={
        "email": "E-mail",
        "person": "Person",
        "first_name": "Fornavn",
        "last_name": "Efternavn",
        "phone": "Telefon",
        "economic_account_number": "E-conomic kontonr.",
        "profile_picture": "Profilbillede",
        "bio": "Om mig",
        "facebook_link": "Facebook",
        "instagram_handle": "Instagram",
        "is_active": "Aktiv",
        "date_joined": "Oprettet",
    },
)
trail.register(
    Person,
    "residents.person",
    name=lambda p: p.full_name,
    label="Person",
    labels={
        "first_name": "Fornavn",
        "last_name": "Efternavn",
        "birthday": "Fødselsdag",
        "move_in_date": "Indflytning",
        "move_out_date": "Udflytning",
        "study": "Studie",
        "sponsor": "Fylgje",
        "fylgje_raw": "Fylgje (fritekst)",
    },
)
trail.register(
    Residency,
    "residents.residency",
    name=lambda r: f"{r.resident.full_name} {r.year}-{r.month:02d}",
    label="Plads på alumnelisten",
    labels={
        "resident": "Person",
        "room": "Værelse",
        "workgroup": "Embedsgruppe",
        "cleaning": "Rengøring",
        "year": "År",
        "month": "Måned",
    },
)
trail.register(
    AdminAccessGrant,
    "residents.admin_access",
    name=lambda g: g.resident.full_name,
    label="Admin-adgang",
    labels={
        "resident": "Beboer",
        "status": "Status",
        "requested_by": "Anmodet af",
        "decided_by": "Afgjort af",
        "decided_at": "Afgjort",
        "created_at": "Oprettet",
    },
)

trail.actions(
    {
        "auth.login": "Loggede ind",
        "auth.login.failed": "Mislykket login",
        "auth.logout": "Loggede ud",
        "auth.password.changed": "Skiftede kodeord",
        "auth.password_reset.requested": "Bad om nyt kodeord",
        "auth.password_reset.completed": "Satte nyt kodeord via link",
        "auth.access.denied": "Blev nægtet adgang",
        "residents.resident.created": "Oprettede beboer",
        "residents.resident.updated": "Rettede beboer",
        "residents.profile.updated": "Rettede sin profil",
        "residents.month_list.copied": "Kopierede alumnelisten",
        "residents.month_list.saved": "Gemte alumnelisten",
        "residents.month_list.added": "Satte beboer på alumnelisten",
        "residents.role.added": "Gav rolle",
        "residents.role.removed": "Fjernede rolle",
        "residents.admin_access.approved": "Godkendte admin-adgang",
        "residents.admin_access.denied": "Afviste admin-adgang",
        "residents.preview.set": "Viste siden som rolle",
        "residents.preview.cleared": "Stoppede visning som rolle",
        "rooms.round.ended": "Afsluttede værelsesrunden",
    }
)
