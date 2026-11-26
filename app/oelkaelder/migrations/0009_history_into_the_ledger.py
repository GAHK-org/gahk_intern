"""Every ølkælder record from before GAHK-konto becomes ledger history (oelkaelder.history): deposits,
purchase shares and adjustments, oldest first, each dated when it happened, the voided ones (the old
`is_valid` flags, dropped in 0010) followed by their reversal. Afterwards every shopper's balance is
what the old records added up to.

Not atomic: each record is written and linked in a transaction of its own, and the import skips what is
linked already, so a run cut off half way (a deploy timing out on a long history) is simply run again.

Also removes the retired monthly ølkælder statement from the database scheduler: GAHK-konto's monthly
statement covers the ølkælder accounts, and the DatabaseScheduler never removes an entry by itself.

Backwards does nothing: the ledger is append-only, so what was written stays (and a second run forwards
finds it all linked and writes nothing).
"""

from django.db import migrations


def import_history(apps, schema_editor):  # noqa: ANN001, ANN201
    from oelkaelder import history

    history.import_history(
        history.Models(
            Shopper=apps.get_model("oelkaelder", "Shopper"),
            Deposit=apps.get_model("oelkaelder", "Deposit"),
            Transaction=apps.get_model("oelkaelder", "Transaction"),
            TransactionItem=apps.get_model("oelkaelder", "TransactionItem"),
            PurchaseShare=apps.get_model("oelkaelder", "PurchaseShare"),
            Adjustment=apps.get_model("oelkaelder", "Adjustment"),
        )
    )


def retire_monthly_statement(apps, schema_editor):  # noqa: ANN001, ANN201
    apps.get_model("django_celery_beat", "PeriodicTask").objects.filter(
        task="oelkaelder.tasks.send_monthly_statements"
    ).delete()


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("oelkaelder", "0008_ledger_links"),
        # The import writes through GAHK-konto's own code, with the app's models of these apps.
        ("credit", "0004_closed_periods"),
        ("residents", "0015_delete_roleassignment"),
        ("core", "0020_drop_arkiv_tables"),
        ("audit", "0002_retention"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [
        migrations.RunPython(import_history, migrations.RunPython.noop),
        migrations.RunPython(retire_monthly_statement, migrations.RunPython.noop),
    ]
