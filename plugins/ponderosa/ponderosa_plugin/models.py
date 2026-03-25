import uuid

from django.db import models


class SyncLedger(models.Model):
    """Maps core-app UUIDs to InvenTree PKs for bidirectional lookup."""

    class Meta:
        app_label = 'ponderosa_plugin'
        unique_together = [('core_entity_type', 'core_id')]
        indexes = [
            models.Index(fields=['inventree_model', 'inventree_pk']),
        ]

    ENTITY_TYPES = [
        ('sales_order', 'Sales Order'),
        ('job', 'Job'),
        ('inventory_item', 'Inventory Item'),
        ('warehouse', 'Warehouse'),
        ('warehouse_location', 'Warehouse Location'),
    ]
    INVENTREE_MODELS = [
        ('SalesOrder', 'Sales Order'),
        ('Build', 'Build Order'),
        ('Part', 'Part'),
        ('StockItem', 'Stock Item'),
        ('StockLocation', 'Stock Location'),
    ]
    SYNC_STATUSES = [
        ('synced', 'Synced'),
        ('pending', 'Pending'),
        ('error', 'Error'),
    ]

    core_entity_type = models.CharField(max_length=32, choices=ENTITY_TYPES)
    core_id = models.UUIDField(db_index=True)
    inventree_model = models.CharField(max_length=32, choices=INVENTREE_MODELS)
    inventree_pk = models.IntegerField()
    last_synced_at = models.DateTimeField(auto_now=True)
    core_updated_at = models.DateTimeField(null=True, blank=True)
    sync_status = models.CharField(max_length=16, choices=SYNC_STATUSES, default='pending')
    error_message = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"{self.core_entity_type}:{self.core_id} -> {self.inventree_model}:{self.inventree_pk}"


class WebhookInbox(models.Model):
    """Buffers inbound events from core-app for reliable, idempotent processing."""

    class Meta:
        app_label = 'ponderosa_plugin'
        ordering = ['received_at']

    event_id = models.UUIDField(unique=True, help_text='Dedup key from core-app event log')
    event_type = models.CharField(max_length=64, help_text='e.g. JOB.UPSERT, SALES_ORDER.STATUS_CHANGE')
    payload = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.IntegerField(default=0)
    last_error = models.TextField(null=True, blank=True)

    def __str__(self):
        status = 'processed' if self.processed_at else f'pending (attempts={self.attempts})'
        return f"{self.event_type} [{self.event_id}] — {status}"


class StockSyncCheckpoint(models.Model):
    """Tracks last pushed stock level per inventory item to detect deltas."""

    class Meta:
        app_label = 'ponderosa_plugin'

    inventory_item_core_id = models.UUIDField(unique=True)
    inventree_part_pk = models.IntegerField(null=True, blank=True)
    last_pushed_quantity = models.IntegerField(default=0)
    last_pushed_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"StockCheckpoint({self.inventory_item_core_id}) qty={self.last_pushed_quantity}"


# ---------------------------------------------------------------------------
# Production Steps
# ---------------------------------------------------------------------------

# Legacy constant kept for reference during data migrations only.
OPERATION_TYPES = [
    ('digital_print', 'Digital Print'),
    ('offset_print', 'Offset Print'),
    ('wide_format', 'Wide Format'),
    ('cut', 'Cut'),
    ('fold', 'Fold'),
    ('score', 'Score'),
    ('perforate', 'Perforate'),
    ('laminate', 'Laminate'),
    ('mount', 'Mount'),
    ('saddle_stitch', 'Saddle Stitch'),
    ('perfect_bind', 'Perfect Bind'),
    ('coil_bind', 'Coil Bind'),
    ('wire_o', 'Wire-O'),
    ('collate', 'Collate'),
    ('pad', 'Pad'),
    ('drill', 'Drill'),
    ('numbering', 'Numbering'),
    ('shrink_wrap', 'Shrink Wrap'),
    ('package', 'Package'),
    ('proof_review', 'Proof/Review'),
    ('qc', 'QC'),
    ('custom', 'Custom'),
]


class StepType(models.Model):
    """User-configurable production step type."""

    class Meta:
        app_label = 'ponderosa_plugin'
        ordering = ['sort_order', 'name']

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True, default='')
    color = models.CharField(
        max_length=7, default='#1971c2',
        help_text='Hex color for UI badges',
    )
    icon = models.CharField(max_length=50, blank=True, default='')
    station_group = models.CharField(
        max_length=50, blank=True, default='',
        help_text='Logical group linking this type to compatible stations',
    )
    is_automatable = models.BooleanField(
        default=False,
        help_text='Whether N8N can trigger execution of this step type',
    )
    sort_order = models.PositiveIntegerField(default=0)
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Station(models.Model):
    """A physical piece of equipment on the shop floor."""

    class Meta:
        app_label = 'ponderosa_plugin'
        ordering = ['name']

    name = models.CharField(max_length=100, unique=True)
    station_type = models.CharField(
        max_length=50, blank=True, default='',
        help_text='Free-text category (e.g. press, finishing, embroidery)',
    )
    active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.name} ({self.station_type})" if self.station_type else self.name


class ProductionStepTemplate(models.Model):
    """Defines a production step on a Part — the recipe for how to make it."""

    class Meta:
        app_label = 'ponderosa_plugin'
        ordering = ['part', 'sequence']
        unique_together = [('part', 'sequence')]

    part = models.ForeignKey(
        'part.Part',
        on_delete=models.CASCADE,
        related_name='production_step_templates',
    )
    sequence = models.PositiveIntegerField()
    step_type = models.ForeignKey(
        StepType,
        on_delete=models.PROTECT,
        related_name='templates',
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    estimated_duration = models.DurationField(null=True, blank=True)
    station_group = models.CharField(
        max_length=50, blank=True, default='',
        help_text='Override station_group from step type for this template',
    )
    is_automatable = models.BooleanField(default=False)
    metadata = models.JSONField(
        default=dict, blank=True,
        help_text='Attachment IDs linking to production files on the Part',
    )

    def effective_station_group(self):
        return self.station_group or self.step_type.station_group

    def __str__(self):
        return f"{self.part} step {self.sequence}: {self.name}"


class BuildOrderStep(models.Model):
    """A trackable production step on a Build Order."""

    class Meta:
        app_label = 'ponderosa_plugin'
        ordering = ['build', 'sequence']
        unique_together = [('build', 'sequence')]
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['station']),
        ]

    STEP_STATUSES = [
        ('pending', 'Pending'),
        ('queued', 'Queued'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('on_hold', 'On Hold'),
        ('blocked', 'Blocked'),
        ('skipped', 'Skipped'),
    ]

    build = models.ForeignKey(
        'build.Build',
        on_delete=models.CASCADE,
        related_name='production_steps',
    )
    template = models.ForeignKey(
        ProductionStepTemplate,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='build_steps',
    )
    sequence = models.PositiveIntegerField()
    step_type = models.ForeignKey(
        StepType,
        on_delete=models.PROTECT,
        related_name='build_steps',
    )
    name = models.CharField(max_length=200)
    station = models.ForeignKey(
        Station,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='assigned_steps',
    )
    status = models.CharField(max_length=20, choices=STEP_STATUSES, default='pending')
    assigned_to = models.ForeignKey(
        'auth.User',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='assigned_build_steps',
    )
    priority = models.PositiveIntegerField(default=0, help_text='Lower = higher priority')
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    operator_notes = models.TextField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Build {self.build_id} step {self.sequence}: {self.name} [{self.status}]"
