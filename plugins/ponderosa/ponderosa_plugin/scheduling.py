"""Scheduled tasks for the Ponderosa plugin.

Handles: webhook inbox processing, stock level pushback, and full reconciliation sync.
"""

import logging

from django.utils import timezone

from plugin.registry import registry

logger = logging.getLogger('ponderosa_plugin')

MAX_INBOX_ATTEMPTS = 5


def _get_plugin():
    return registry.get_plugin('ponderosa')


def _get_client():
    """Build a CoreAppClient from plugin settings."""
    from ponderosa_plugin.sync_engine import CoreAppClient

    plugin = _get_plugin()
    if not plugin:
        return None

    base_url = plugin.get_setting('PORTAL_API_URL')
    api_key = plugin.get_setting('PORTAL_API_KEY')
    if not base_url or not api_key:
        logger.warning("Core-app API URL or key not configured — skipping sync")
        return None

    return CoreAppClient(base_url, api_key)


def process_webhook_inbox():
    """Process pending WebhookInbox records.

    Routes events to the appropriate sync handler based on event_type.
    Runs every 1 minute via SCHEDULED_TASKS.
    """
    from ponderosa_plugin.models import WebhookInbox
    from ponderosa_plugin.sync_engine import (
        BuildOrderSyncHandler,
        SalesOrderSyncHandler,
        PartSyncHandler,
    )

    client = _get_client()
    pending = WebhookInbox.objects.filter(
        processed_at__isnull=True,
        attempts__lt=MAX_INBOX_ATTEMPTS,
    ).order_by('received_at')[:50]

    if not pending:
        return

    logger.info("Processing %d pending webhook events", len(pending))

    for record in pending:
        record.attempts += 1
        try:
            _dispatch_event(record, client)
            record.processed_at = timezone.now()
            record.last_error = None
        except Exception as e:
            logger.exception("Failed to process webhook event %s: %s", record.event_id, e)
            record.last_error = str(e)[:2000]
        record.save()


def _dispatch_event(record, client):
    """Route a WebhookInbox record to the appropriate handler."""
    from ponderosa_plugin.sync_engine import (
        BuildOrderSyncHandler,
        SalesOrderSyncHandler,
        PartSyncHandler,
        ShipmentSyncHandler,
    )

    event_type = record.event_type
    payload = record.payload

    if event_type == 'JOB.UPSERT':
        BuildOrderSyncHandler.sync(payload, client)
    elif event_type == 'JOB.STATUS_CHANGE':
        BuildOrderSyncHandler.update_status(payload)
    elif event_type == 'JOB.DELETE':
        _handle_job_delete(payload)
    elif event_type == 'SALES_ORDER.UPSERT':
        SalesOrderSyncHandler.sync(payload, client)
    elif event_type == 'SALES_ORDER.STATUS_CHANGE':
        SalesOrderSyncHandler.update_status(payload)
    elif event_type == 'INVENTORY_ITEM.UPSERT':
        PartSyncHandler.sync(payload)
    elif event_type == 'SHIPMENT.UPSERT':
        ShipmentSyncHandler.sync(payload)
    else:
        logger.warning("Unhandled webhook event type: %s", event_type)


def _handle_job_delete(payload: dict):
    """Handle deletion of a job — cancel the corresponding Build Order."""
    from build.models import Build
    from ponderosa_plugin.models import SyncLedger

    resource_id = payload.get('resourceId')
    ledger = SyncLedger.objects.filter(
        core_entity_type='job', core_id=resource_id
    ).first()
    if not ledger:
        return

    try:
        build = Build.objects.get(pk=ledger.inventree_pk)
        build.status = 40  # CANCELLED
        build.save()
        ledger.sync_status = 'synced'
        ledger.save()
        logger.info("Cancelled Build %s for deleted job %s", build.pk, resource_id)
    except Build.DoesNotExist:
        ledger.delete()


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


def full_sync():
    """Full reconciliation sync — catch-up for missed webhooks.

    Fetches active jobs and sales orders from core-app and ensures
    corresponding InvenTree objects exist.
    """
    from ponderosa_plugin.sync_engine import (
        BuildOrderSyncHandler,
        SalesOrderSyncHandler,
    )
    from ponderosa_plugin.models import SyncLedger

    client = _get_client()
    if not client:
        return

    # Reconcile: check for SyncLedger entries in 'error' state and retry
    error_entries = SyncLedger.objects.filter(sync_status='error')[:20]
    retried = 0
    for entry in error_entries:
        try:
            if entry.core_entity_type == 'job':
                job_data = client.get_job(str(entry.core_id))
                payload = {
                    'resourceId': str(entry.core_id),
                    'attributes': job_data,
                }
                BuildOrderSyncHandler.sync(payload, client)
                retried += 1
            elif entry.core_entity_type == 'sales_order':
                so_data = client.get_sales_order(str(entry.core_id))
                payload = {
                    'resourceId': str(entry.core_id),
                    'attributes': so_data,
                }
                SalesOrderSyncHandler.sync(payload, client)
                retried += 1
        except Exception as e:
            logger.warning("full_sync retry failed for %s %s: %s", entry.core_entity_type, entry.core_id, e)

    if retried:
        logger.info("full_sync retried %d error entries", retried)
