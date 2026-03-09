"""Scheduled tasks for the Ponderosa plugin.

Only task: push InvenTree stock levels to core-app as a read-only mirror.
Entity creation (Build Orders, SalesOrders, Parts) is handled by N8N workflows.
"""

import logging

from plugin.registry import registry

logger = logging.getLogger('ponderosa_plugin')


def _get_client():
    """Build a CoreAppClient from plugin settings."""
    from ponderosa_plugin.sync_engine import CoreAppClient

    plugin = registry.get_plugin('ponderosa')
    if not plugin:
        return None

    base_url = plugin.get_setting('PORTAL_API_URL')
    api_key = plugin.get_setting('PORTAL_API_KEY')
    if not base_url or not api_key:
        logger.warning("Core-app API URL or key not configured — skipping stock push")
        return None

    return CoreAppClient(base_url, api_key)


def push_stock_levels():
    """Push current InvenTree stock levels to core-app.

    Compares current totals against StockSyncCheckpoint to only push deltas.
    Runs every 10 minutes via SCHEDULED_TASKS.
    """
    from django.db import models as db_models
    from stock.models import StockItem
    from ponderosa_plugin.models import SyncLedger, StockSyncCheckpoint

    client = _get_client()
    if not client:
        return

    # Find all inventory items that have been synced to InvenTree Parts
    ledger_entries = SyncLedger.objects.filter(
        core_entity_type='inventory_item',
        inventree_model='Part',
        sync_status='synced',
    )

    pushed = 0
    for entry in ledger_entries:
        # Sum all stock for this Part
        total_qty = StockItem.objects.filter(
            part_id=entry.inventree_pk
        ).aggregate(
            total=db_models.Sum('quantity')
        )['total'] or 0

        # Check if changed since last push
        checkpoint, created = StockSyncCheckpoint.objects.get_or_create(
            inventory_item_core_id=entry.core_id,
            defaults={'inventree_part_pk': entry.inventree_pk, 'last_pushed_quantity': -1},
        )

        if checkpoint.last_pushed_quantity != total_qty:
            try:
                client.push_stock_level(str(entry.core_id), int(total_qty))
                checkpoint.last_pushed_quantity = int(total_qty)
                checkpoint.inventree_part_pk = entry.inventree_pk
                checkpoint.save()
                pushed += 1
            except Exception as e:
                logger.warning("Failed to push stock for %s: %s", entry.core_id, e)

    if pushed:
        logger.info("Pushed %d stock level updates to core-app", pushed)
