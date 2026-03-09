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
