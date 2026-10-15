from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0053_ow_poll_watermark"),
    ]

    operations = [
        migrations.AddField(
            model_name="observation",
            name="created",
            field=models.DateTimeField(null=True),
        ),
        # The nearest thing to an arrival time an existing reading has.
        migrations.RunSQL("UPDATE core_observation SET created = last_updated", reverse_sql=migrations.RunSQL.noop),
        migrations.AlterField(
            model_name="observation",
            name="created",
            field=models.DateTimeField(auto_now_add=True),
        ),
    ]
