"""What the audit trail keeps of residents, their months and roles, and of logging in."""

from audit import trail

from .models import AdminAccessGrant, Residency, Resident, RoleAssignment

trail.register(
    Resident,
    "residents.resident",
    name=lambda r: r.full_name or r.email,
    label="Beboer",
    # last_login moves at every login, which is its own event; is_staff follows the roles, the
    # thumbnail follows the picture and access_level follows the alumneliste and the date (access.levels),
    # all kept by code rather than by anyone's choice.
    ignore={"last_login", "is_staff", "profile_thumbnail", "access_level", "groups", "user_permissions"},
    redact={"password"},
    labels={
        "email": "E-mail",
        "first_name": "Fornavn",
        "last_name": "Efternavn",
        "phone": "Telefon",
        "birthday": "Fødselsdag",
        "move_in_date": "Indflytning",
        "move_out_date": "Udflytning",
        "study": "Studie",
        "sponsor": "Fylgje",
        "fylgje_raw": "Fylgje (fritekst)",
        "economic_account_number": "E-conomic kontonr.",
        "profile_picture": "Profilbillede",
        "bio": "Om mig",
        "facebook_link": "Facebook",
        "instagram_handle": "Instagram",
        "is_active": "Aktiv",
        "is_kitchen": "Køkkenkonto",
        "access_level_override": "Adgang (undtagelse)",
        "access_level_override_until": "Undtagelsen gælder til og med",
        "date_joined": "Oprettet",
    },
)
trail.register(
    Residency,
    "residents.residency",
    name=lambda r: f"{r.resident.full_name} {r.year}-{r.month:02d}",
    label="Plads på alumnelisten",
    labels={
        "resident": "Beboer",
        "room": "Værelse",
        "workgroup": "Embedsgruppe",
        "cleaning": "Rengøring",
        "year": "År",
        "month": "Måned",
    },
)
trail.register(
    RoleAssignment,
    "residents.role",
    name=lambda r: f"{r.resident.full_name}: {r.get_role_display()} {r.year}-{r.month:02d}",
    label="Rolle",
    labels={"resident": "Beboer", "role": "Rolle", "year": "År", "month": "Måned"},
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
        "residents.resident.access_level_overridden": "Gav beboer en undtagelse for adgang",
        "residents.resident.access_level_override_expired": "Adgangsundtagelsen udløb",
        "residents.profile.updated": "Rettede sin profil",
        "residents.month_list.copied": "Kopierede alumnelisten",
        "residents.month_list.saved": "Gemte alumnelisten",
        "residents.month_list.added": "Satte beboer på alumnelisten",
        "residents.role.added": "Gav rolle",
        "residents.role.removed": "Fjernede rolle",
        "residents.admin_access.approved": "Godkendte admin-adgang",
        "residents.admin_access.denied": "Afviste admin-adgang",
        "residents.preview.set": "Viste siden som rolle eller adgangsniveau",
        "residents.preview.cleared": "Stoppede visning som rolle",
        "rooms.round.ended": "Afsluttede værelsesrunden",
    }
)
