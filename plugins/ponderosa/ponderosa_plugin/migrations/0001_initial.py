"""Initial migration — creates all ponderosa_plugin models.

Handles the case where tables may already exist from AppMixin
auto-creation (before migrations were added to this plugin).
Old production tables (Station, ProductionStepTemplate, BuildOrderStep)
are dropped because the schema changed (operation_type -> step_type FK).
Sync tables are backed up and restored to preserve existing data.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import connection, migrations, models


def _table_exists(cursor, table_name):
    cursor.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = %s)",
        [table_name],
    )
    return cursor.fetchone()[0]


def drop_old_tables(apps, schema_editor):
    """Drop all old plugin tables so CreateModel can recreate them cleanly.

    Sync table data is backed up to temp tables and restored after migration.
    """
    with connection.cursor() as cursor:
        # Back up sync data if tables exist
        for table in ('syncledger', 'webhookinbox', 'stocksynccheckpoint'):
            full = f'ponderosa_plugin_{table}'
            if _table_exists(cursor, full):
                cursor.execute(f'CREATE TEMP TABLE _backup_{table} AS SELECT * FROM {full}')

        # Drop everything (order matters for FKs)
        for table in (
            'ponderosa_plugin_buildorderstep',
            'ponderosa_plugin_productionsteptemplate',
            'ponderosa_plugin_station',
            'ponderosa_plugin_steptype',
            'ponderosa_plugin_stocksynccheckpoint',
            'ponderosa_plugin_webhookinbox',
            'ponderosa_plugin_syncledger',
        ):
            cursor.execute(f'DROP TABLE IF EXISTS {table} CASCADE')


def restore_sync_data(apps, schema_editor):
    """Restore sync table data from temp backup tables."""
    with connection.cursor() as cursor:
        for table in ('syncledger', 'webhookinbox', 'stocksynccheckpoint'):
            temp = f'_backup_{table}'
            full = f'ponderosa_plugin_{table}'
            # Check if temp table exists
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_tables WHERE tablename = %s)",
                [temp],
            )
            if cursor.fetchone()[0]:
                cursor.execute(f'INSERT INTO {full} SELECT * FROM {temp}')
                cursor.execute(f'DROP TABLE {temp}')
                # Reset sequence to max id
                cursor.execute(
                    f"SELECT setval(pg_get_serial_sequence('{full}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {full}), 1))"
                )


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ── Drop old tables, backup sync data ─────────────────────────
        migrations.RunPython(drop_old_tables, migrations.RunPython.noop),

        # ── Sync models ──────────────────────────────────────────────

        migrations.CreateModel(
            name='SyncLedger',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('core_entity_type', models.CharField(choices=[
                    ('sales_order', 'Sales Order'), ('job', 'Job'),
                    ('inventory_item', 'Inventory Item'), ('warehouse', 'Warehouse'),
                    ('warehouse_location', 'Warehouse Location'),
                ], max_length=32)),
                ('core_id', models.UUIDField(db_index=True)),
                ('inventree_model', models.CharField(choices=[
                    ('SalesOrder', 'Sales Order'), ('Build', 'Build Order'),
                    ('Part', 'Part'), ('StockItem', 'Stock Item'),
                    ('StockLocation', 'Stock Location'),
                ], max_length=32)),
                ('inventree_pk', models.IntegerField()),
                ('last_synced_at', models.DateTimeField(auto_now=True)),
                ('core_updated_at', models.DateTimeField(blank=True, null=True)),
                ('sync_status', models.CharField(choices=[
                    ('synced', 'Synced'), ('pending', 'Pending'), ('error', 'Error'),
                ], default='pending', max_length=16)),
                ('error_message', models.TextField(blank=True, null=True)),
            ],
            options={
                'app_label': 'ponderosa_plugin',
                'unique_together': {('core_entity_type', 'core_id')},
            },
        ),
        migrations.AddIndex(
            model_name='syncledger',
            index=models.Index(fields=['inventree_model', 'inventree_pk'],
                               name='ponderosa_p_inventr_idx'),
        ),

        migrations.CreateModel(
            name='WebhookInbox',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_id', models.UUIDField(help_text='Dedup key from core-app event log', unique=True)),
                ('event_type', models.CharField(help_text='e.g. JOB.UPSERT, SALES_ORDER.STATUS_CHANGE', max_length=64)),
                ('payload', models.JSONField()),
                ('received_at', models.DateTimeField(auto_now_add=True)),
                ('processed_at', models.DateTimeField(blank=True, null=True)),
                ('attempts', models.IntegerField(default=0)),
                ('last_error', models.TextField(blank=True, null=True)),
            ],
            options={
                'app_label': 'ponderosa_plugin',
                'ordering': ['received_at'],
            },
        ),

        migrations.CreateModel(
            name='StockSyncCheckpoint',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('inventory_item_core_id', models.UUIDField(unique=True)),
                ('inventree_part_pk', models.IntegerField(blank=True, null=True)),
                ('last_pushed_quantity', models.IntegerField(default=0)),
                ('last_pushed_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'app_label': 'ponderosa_plugin',
            },
        ),

        # ── Restore sync data from backup ─────────────────────────────
        migrations.RunPython(restore_sync_data, migrations.RunPython.noop),

        # ── StepType (new) ───────────────────────────────────────────

        migrations.CreateModel(
            name='StepType',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, unique=True)),
                ('slug', models.SlugField(max_length=100, unique=True)),
                ('description', models.TextField(blank=True, default='')),
                ('color', models.CharField(default='#1971c2', help_text='Hex color for UI badges', max_length=7)),
                ('icon', models.CharField(blank=True, default='', max_length=50)),
                ('station_group', models.CharField(blank=True, default='', help_text='Logical group linking this type to compatible stations', max_length=50)),
                ('is_automatable', models.BooleanField(default=False, help_text='Whether N8N can trigger execution of this step type')),
                ('sort_order', models.PositiveIntegerField(default=0)),
                ('active', models.BooleanField(default=True)),
            ],
            options={
                'app_label': 'ponderosa_plugin',
                'ordering': ['sort_order', 'name'],
            },
        ),

        # ── Station ──────────────────────────────────────────────────

        migrations.CreateModel(
            name='Station',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, unique=True)),
                ('station_type', models.CharField(blank=True, default='', help_text='Free-text category (e.g. press, finishing, embroidery)', max_length=50)),
                ('active', models.BooleanField(default=True)),
                ('metadata', models.JSONField(blank=True, default=dict)),
            ],
            options={
                'app_label': 'ponderosa_plugin',
                'ordering': ['name'],
            },
        ),

        # ── ProductionStepTemplate ───────────────────────────────────

        migrations.CreateModel(
            name='ProductionStepTemplate',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sequence', models.PositiveIntegerField()),
                ('name', models.CharField(max_length=200)),
                ('description', models.TextField(blank=True, default='')),
                ('estimated_duration', models.DurationField(blank=True, null=True)),
                ('station_group', models.CharField(blank=True, default='', help_text='Override station_group from step type for this template', max_length=50)),
                ('is_automatable', models.BooleanField(default=False)),
                ('metadata', models.JSONField(blank=True, default=dict, help_text='Attachment IDs linking to production files on the Part')),
                ('part', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='production_step_templates', to='part.part')),
                ('step_type', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='templates', to='ponderosa_plugin.steptype')),
            ],
            options={
                'app_label': 'ponderosa_plugin',
                'ordering': ['part', 'sequence'],
                'unique_together': {('part', 'sequence')},
            },
        ),

        # ── BuildOrderStep ───────────────────────────────────────────

        migrations.CreateModel(
            name='BuildOrderStep',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sequence', models.PositiveIntegerField()),
                ('name', models.CharField(max_length=200)),
                ('status', models.CharField(choices=[
                    ('pending', 'Pending'), ('queued', 'Queued'),
                    ('in_progress', 'In Progress'), ('completed', 'Completed'),
                    ('on_hold', 'On Hold'), ('blocked', 'Blocked'),
                    ('skipped', 'Skipped'),
                ], default='pending', max_length=20)),
                ('priority', models.PositiveIntegerField(default=0, help_text='Lower = higher priority')),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('operator_notes', models.TextField(blank=True, null=True)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('build', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='production_steps', to='build.build')),
                ('template', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='build_steps', to='ponderosa_plugin.productionsteptemplate')),
                ('step_type', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='build_steps', to='ponderosa_plugin.steptype')),
                ('station', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='assigned_steps', to='ponderosa_plugin.station')),
                ('assigned_to', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='assigned_build_steps', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'app_label': 'ponderosa_plugin',
                'ordering': ['build', 'sequence'],
                'unique_together': {('build', 'sequence')},
            },
        ),
        migrations.AddIndex(
            model_name='buildorderstep',
            index=models.Index(fields=['status'], name='ponderosa_p_status_idx'),
        ),
        migrations.AddIndex(
            model_name='buildorderstep',
            index=models.Index(fields=['station'], name='ponderosa_p_station_idx'),
        ),
    ]
