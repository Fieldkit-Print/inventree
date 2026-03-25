"""Seed default StepType records from the legacy OPERATION_TYPES list."""

from django.db import migrations


SEED_STEP_TYPES = [
    # (slug, name, station_group, color, sort_order)
    ('digital_print', 'Digital Print', 'press', '#1971c2', 1),
    ('offset_print', 'Offset Print', 'press', '#1864ab', 2),
    ('wide_format', 'Wide Format', 'press', '#0c8599', 3),
    ('cut', 'Cut', 'finishing', '#e8590c', 4),
    ('fold', 'Fold', 'finishing', '#d9480f', 5),
    ('score', 'Score', 'finishing', '#c2255c', 6),
    ('perforate', 'Perforate', 'finishing', '#9c36b5', 7),
    ('laminate', 'Laminate', 'finishing', '#6741d9', 8),
    ('mount', 'Mount', 'finishing', '#3b5bdb', 9),
    ('saddle_stitch', 'Saddle Stitch', 'binding', '#2b8a3e', 10),
    ('perfect_bind', 'Perfect Bind', 'binding', '#2f9e44', 11),
    ('coil_bind', 'Coil Bind', 'binding', '#37b24d', 12),
    ('wire_o', 'Wire-O', 'binding', '#40c057', 13),
    ('collate', 'Collate', 'finishing', '#5c940d', 14),
    ('pad', 'Pad', 'finishing', '#74b816', 15),
    ('drill', 'Drill', 'finishing', '#868e96', 16),
    ('numbering', 'Numbering', 'press', '#495057', 17),
    ('shrink_wrap', 'Shrink Wrap', 'packaging', '#fab005', 18),
    ('package', 'Package', 'packaging', '#f59f00', 19),
    ('proof_review', 'Proof/Review', 'qc', '#fd7e14', 20),
    ('qc', 'QC', 'qc', '#e03131', 21),
    ('custom', 'Custom', '', '#868e96', 22),
]


def seed_step_types(apps, schema_editor):
    StepType = apps.get_model('ponderosa_plugin', 'StepType')
    for slug, name, group, color, order in SEED_STEP_TYPES:
        StepType.objects.get_or_create(
            slug=slug,
            defaults={
                'name': name,
                'station_group': group,
                'color': color,
                'sort_order': order,
            },
        )


def remove_step_types(apps, schema_editor):
    StepType = apps.get_model('ponderosa_plugin', 'StepType')
    slugs = [s[0] for s in SEED_STEP_TYPES]
    StepType.objects.filter(slug__in=slugs).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('ponderosa_plugin', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_step_types, remove_step_types),
    ]
