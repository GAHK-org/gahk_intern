"""AkAutoApply used to record the month that was charged as it STARTED; it now records the last month
charged once it was OVER. Step a set marker back one month so the month in progress at deploy (already
charged early, from the copied-forward list) still counts as uncharged and is reconciled against its
final list on the next 1st — by the lazy backstop too, not only the scheduled task."""

from django.db import migrations


def step_back(apps, schema_editor) -> None:  # noqa: ANN001
    AkAutoApply = apps.get_model("ak", "AkAutoApply")
    for state in AkAutoApply.objects.exclude(year=None).exclude(month=None):
        state.year, state.month = (state.year - 1, 12) if state.month == 1 else (state.year, state.month - 1)
        state.save(update_fields=["year", "month"])


def step_forward(apps, schema_editor) -> None:  # noqa: ANN001
    AkAutoApply = apps.get_model("ak", "AkAutoApply")
    for state in AkAutoApply.objects.exclude(year=None).exclude(month=None):
        state.year, state.month = (state.year + 1, 1) if state.month == 12 else (state.year, state.month + 1)
        state.save(update_fields=["year", "month"])


class Migration(migrations.Migration):
    dependencies = [
        ("ak", "0003_akautoapply"),
    ]

    operations = [
        migrations.RunPython(step_back, step_forward),
    ]
