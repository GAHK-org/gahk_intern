"""What the wiki's read-only Postgres role (`wiki_ro`) may read, kept with the schema it reads.

The wiki (wiki-deploy/extensions/GahkAuthLogin) logs residents in against residents_resident, checks
on every request that the intranet still lets them in, and makes Netvaerksgruppen (an approved
`administrator` of the active month) its administrators. Column grants, not table grants: the role
sees nothing else about a resident.

Does nothing where no `wiki_ro` role exists (dev, tests). A role created after this has run needs the
same GRANTs by hand. Reversing takes back only what the per-request check added; the login columns
stay, since the wiki cannot log anyone in without them.
"""

from django.db import migrations

LOGIN = (
    "GRANT SELECT (id, email, password, first_name, last_name, is_active) ON residents_resident TO wiki_ro;"
)
STANDING = """
    GRANT SELECT (is_kitchen, is_superuser) ON residents_resident TO wiki_ro;
    GRANT SELECT (resident_id, role, year, month) ON residents_roleassignment TO wiki_ro;
    GRANT SELECT (year, month) ON residents_residency TO wiki_ro;
    GRANT SELECT (resident_id, status) ON residents_adminaccessgrant TO wiki_ro;
"""
REVOKE_STANDING = """
    REVOKE SELECT (is_kitchen, is_superuser) ON residents_resident FROM wiki_ro;
    REVOKE SELECT (resident_id, role, year, month) ON residents_roleassignment FROM wiki_ro;
    REVOKE SELECT (year, month) ON residents_residency FROM wiki_ro;
    REVOKE SELECT (resident_id, status) ON residents_adminaccessgrant FROM wiki_ro;
"""


def if_role_exists(statements: str) -> str:
    return f"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wiki_ro') THEN
        {statements}
    END IF;
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("residents", "0012_resident_economic_account_number"),
    ]

    operations = [
        migrations.RunSQL(if_role_exists(LOGIN + STANDING), if_role_exists(REVOKE_STANDING)),
    ]
