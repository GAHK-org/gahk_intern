"""Drop den_hurtige's ownership of PushSubscription. State only — core/0005 moved the real table.

Depends on core/0005 so the CreateModel there always runs first: with two migrations describing the
same table, the order is the difference between a clean state move and Django trying to create a
table that already exists.

Numbered 0006 rather than 0005 because dev's channel work landed a 0005 in this app in the meantime.
Two 0005 leaves is not a stylistic problem — Django refuses to migrate a graph with more than one
leaf per app at all — so this depends on that one and linearises the graph behind it. The two are
independent in substance (channels touch QuickPost and ChannelMute; this touches PushSubscription),
so the order between them carries no meaning beyond making the graph a line.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("den_hurtige", "0005_channelmute_quickpost_channel_and_more"),
        ("core", "0005_pushsubscription"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.DeleteModel(name="PushSubscription")],
            database_operations=[],
        ),
    ]
