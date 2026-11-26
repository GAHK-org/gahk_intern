"""A closed period takes nothing more: the database refuses a posting whose transfer was made before
the end of its book's latest closing (credit.closing). A correction goes into the open period."""

from django.db import migrations

from core.append_only import trigger_sql

APPEND_ONLY, APPEND_ONLY_REVERSE = trigger_sql(
    "credit_records_append_only",
    ("credit_anchor", "credit_anchordelivery", "credit_closing", "credit_closingbalance", "credit_statement"),
    "The GAHK-konto records",
)

RULES = [
    """
    CREATE FUNCTION credit_posting_open_period() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
        made timestamptz;
        closed_until timestamptz;
    BEGIN
        SELECT created_at INTO made FROM credit_transfer WHERE id = NEW.transfer_id;
        SELECT max(c.ends_at) INTO closed_until
            FROM credit_closing c JOIN credit_creditaccount a ON a.book_id = c.book_id
            WHERE a.id = NEW.account_id;
        IF closed_until IS NOT NULL AND made < closed_until THEN
            RAISE EXCEPTION 'GAHK-konto: the period up to % is closed for account %', closed_until, NEW.account_id;
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    (
        "CREATE TRIGGER credit_posting_open_period BEFORE INSERT ON credit_posting "
        "FOR EACH ROW EXECUTE FUNCTION credit_posting_open_period()"
    ),
]
RULES_REVERSE = [
    "DROP TRIGGER credit_posting_open_period ON credit_posting",
    "DROP FUNCTION credit_posting_open_period()",
]


class Migration(migrations.Migration):
    dependencies = [("credit", "0003_closings_anchors_statements")]

    operations = [
        migrations.RunSQL(APPEND_ONLY, reverse_sql=APPEND_ONLY_REVERSE),
        migrations.RunSQL(RULES, reverse_sql=RULES_REVERSE),
    ]
