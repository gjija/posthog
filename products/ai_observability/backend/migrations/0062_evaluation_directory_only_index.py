from django.db import migrations, models

from posthog.migration_helpers import SafeAddIndexConcurrently


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("ai_observability", "0061_backfill_condition_ids"),
    ]

    operations = [
        SafeAddIndexConcurrently(
            model_name="evaluation",
            index=models.Index(
                condition=models.Q(("directory__isnull", False)),
                fields=["directory"],
                name="llma_eval_directory_idx",
            ),
        ),
    ]
