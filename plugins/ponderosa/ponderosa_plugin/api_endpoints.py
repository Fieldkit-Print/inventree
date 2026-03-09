"""Custom API endpoints for UI panels and sync status.

Provides endpoints that the UserInterfaceMixin panels call to fetch
core-app data for display alongside InvenTree objects.
"""

import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from plugin.registry import registry

from ponderosa_plugin.models import SyncLedger, WebhookInbox, StockSyncCheckpoint

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
        return None

    return CoreAppClient(base_url, api_key)


@require_GET
def job_detail(request, build_pk):
    """Fetch core-app Job data for a Build Order's UI panel."""
    ledger = SyncLedger.objects.filter(
        inventree_model='Build', inventree_pk=build_pk
    ).first()

    if not ledger:
        return JsonResponse({
            'linked': False,
            'message': 'This Build Order is not linked to a core-app Job',
        })

    client = _get_client()
    if not client:
        return JsonResponse({
            'linked': True,
            'core_id': str(ledger.core_id),
            'error': 'Core-app API not configured',
        })

    try:
        job_data = client.get_job(str(ledger.core_id))
        return JsonResponse({
            'linked': True,
            'core_id': str(ledger.core_id),
            'job': job_data,
        })
    except Exception as e:
        logger.warning("Failed to fetch job %s from core-app: %s", ledger.core_id, e)
        return JsonResponse({
            'linked': True,
            'core_id': str(ledger.core_id),
            'error': str(e),
        }, status=502)


@require_GET
def order_detail(request, so_pk):
    """Fetch core-app SalesOrder data for a Sales Order's UI panel."""
    ledger = SyncLedger.objects.filter(
        inventree_model='SalesOrder', inventree_pk=so_pk
    ).first()

    if not ledger:
        return JsonResponse({
            'linked': False,
            'message': 'This Sales Order is not linked to a core-app order',
        })

    client = _get_client()
    if not client:
        return JsonResponse({
            'linked': True,
            'core_id': str(ledger.core_id),
            'error': 'Core-app API not configured',
        })

    try:
        order_data = client.get_sales_order(str(ledger.core_id))
        return JsonResponse({
            'linked': True,
            'core_id': str(ledger.core_id),
            'salesOrder': order_data,
        })
    except Exception as e:
        logger.warning("Failed to fetch SO %s from core-app: %s", ledger.core_id, e)
        return JsonResponse({
            'linked': True,
            'core_id': str(ledger.core_id),
            'error': str(e),
        }, status=502)


@require_GET
def inventory_sync_status(request, part_pk):
    """Show sync status for a Part — core-app qty vs InvenTree qty."""
    from django.db import models as db_models
    from stock.models import StockItem

    ledger = SyncLedger.objects.filter(
        inventree_model='Part', inventree_pk=part_pk
    ).first()

    if not ledger:
        return JsonResponse({
            'linked': False,
            'message': 'This Part is not linked to a core-app InventoryItem',
        })

    # Current InvenTree stock total
    inventree_qty = StockItem.objects.filter(
        part_id=part_pk
    ).aggregate(
        total=db_models.Sum('quantity')
    )['total'] or 0

    # Last pushed checkpoint
    checkpoint = StockSyncCheckpoint.objects.filter(
        inventory_item_core_id=ledger.core_id
    ).first()

    return JsonResponse({
        'linked': True,
        'core_id': str(ledger.core_id),
        'inventree_quantity': int(inventree_qty),
        'last_pushed_quantity': checkpoint.last_pushed_quantity if checkpoint else None,
        'last_pushed_at': checkpoint.last_pushed_at.isoformat() if checkpoint and checkpoint.last_pushed_at else None,
        'sync_status': ledger.sync_status,
        'last_synced_at': ledger.last_synced_at.isoformat() if ledger.last_synced_at else None,
    })


@require_GET
def sync_dashboard(request):
    """Comprehensive sync dashboard data."""
    from django.utils import timezone
    from django.db import models as db_models

    now = timezone.now()

    # Inbox stats
    total_received = WebhookInbox.objects.count()
    total_processed = WebhookInbox.objects.filter(processed_at__isnull=False).count()
    pending = WebhookInbox.objects.filter(processed_at__isnull=True).count()
    failed = WebhookInbox.objects.filter(processed_at__isnull=True, attempts__gte=5).count()

    # Recent events
    recent_events = list(
        WebhookInbox.objects.order_by('-received_at')[:20].values(
            'event_id', 'event_type', 'received_at', 'processed_at', 'attempts', 'last_error'
        )
    )
    # Serialize datetimes
    for evt in recent_events:
        for key in ('received_at', 'processed_at'):
            if evt[key]:
                evt[key] = evt[key].isoformat()
        evt['event_id'] = str(evt['event_id'])

    # Ledger summary
    ledger_summary = {}
    for entity_type in ['sales_order', 'job', 'inventory_item']:
        entries = SyncLedger.objects.filter(core_entity_type=entity_type)
        ledger_summary[entity_type] = {
            'total': entries.count(),
            'synced': entries.filter(sync_status='synced').count(),
            'pending': entries.filter(sync_status='pending').count(),
            'error': entries.filter(sync_status='error').count(),
        }

    # Errors
    recent_errors = list(
        SyncLedger.objects.filter(
            sync_status='error'
        ).order_by('-last_synced_at')[:10].values(
            'core_entity_type', 'core_id', 'inventree_model', 'inventree_pk',
            'error_message', 'last_synced_at'
        )
    )
    for err in recent_errors:
        err['core_id'] = str(err['core_id'])
        if err['last_synced_at']:
            err['last_synced_at'] = err['last_synced_at'].isoformat()

    return JsonResponse({
        'timestamp': now.isoformat(),
        'inbox': {
            'total_received': total_received,
            'total_processed': total_processed,
            'pending': pending,
            'failed': failed,
        },
        'sync_ledger': ledger_summary,
        'recent_events': recent_events,
        'recent_errors': recent_errors,
    })


@csrf_exempt
@require_POST
def trigger_initial_import(request):
    """Trigger a one-time initial import of all core-app data.

    POST /plugin/ponderosa/api/initial-import/
    """
    from ponderosa_plugin.sync_engine import InitialImportHandler

    client = _get_client()
    if not client:
        return JsonResponse({
            'error': 'Core-app API not configured',
        }, status=503)

    try:
        result = InitialImportHandler.run(client)
        return JsonResponse({
            'status': 'complete',
            'imported': result,
        })
    except Exception as e:
        logger.exception("Initial import failed: %s", e)
        return JsonResponse({
            'error': str(e),
        }, status=500)
