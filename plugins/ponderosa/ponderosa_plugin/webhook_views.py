"""API endpoints for N8N integration and sync status.

N8N handles entity creation in InvenTree. These endpoints let N8N:
- Register SyncLedger mappings after creating entities
- Query existing mappings
- Check sync health
"""

import json
import logging
import uuid

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET

from ponderosa_plugin.models import SyncLedger

logger = logging.getLogger('ponderosa_plugin')


@csrf_exempt
@require_POST
def register_sync_mapping(request):
    """Register a SyncLedger mapping after N8N creates an entity in InvenTree.

    POST /plugin/ponderosa/api/sync-mapping/
    Body: {
        "core_entity_type": "job",
        "core_id": "uuid-string",
        "inventree_model": "Build",
        "inventree_pk": 123
    }
    """
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    core_entity_type = body.get('core_entity_type')
    core_id = body.get('core_id')
    inventree_model = body.get('inventree_model')
    inventree_pk = body.get('inventree_pk')

    if not all([core_entity_type, core_id, inventree_model, inventree_pk]):
        return JsonResponse({
            'error': 'Missing required fields: core_entity_type, core_id, inventree_model, inventree_pk',
        }, status=400)

    try:
        core_uuid = uuid.UUID(str(core_id))
    except ValueError:
        return JsonResponse({'error': 'Invalid core_id UUID format'}, status=400)

    valid_entity_types = {t[0] for t in SyncLedger.ENTITY_TYPES}
    if core_entity_type not in valid_entity_types:
        return JsonResponse({
            'error': f'Invalid core_entity_type. Must be one of: {", ".join(sorted(valid_entity_types))}',
        }, status=400)

    valid_models = {m[0] for m in SyncLedger.INVENTREE_MODELS}
    if inventree_model not in valid_models:
        return JsonResponse({
            'error': f'Invalid inventree_model. Must be one of: {", ".join(sorted(valid_models))}',
        }, status=400)

    ledger, created = SyncLedger.objects.update_or_create(
        core_entity_type=core_entity_type,
        core_id=core_uuid,
        defaults={
            'inventree_model': inventree_model,
            'inventree_pk': int(inventree_pk),
            'sync_status': 'synced',
            'error_message': None,
        },
    )

    return JsonResponse({
        'status': 'created' if created else 'updated',
        'id': ledger.pk,
        'core_entity_type': ledger.core_entity_type,
        'core_id': str(ledger.core_id),
        'inventree_model': ledger.inventree_model,
        'inventree_pk': ledger.inventree_pk,
    }, status=201 if created else 200)


@require_GET
def lookup_sync_mapping(request):
    """Look up a SyncLedger mapping.

    GET /plugin/ponderosa/api/sync-mapping/?core_id=<uuid>
    GET /plugin/ponderosa/api/sync-mapping/?inventree_model=Build&inventree_pk=123
    """
    core_id = request.GET.get('core_id')
    inventree_model = request.GET.get('inventree_model')
    inventree_pk = request.GET.get('inventree_pk')

    if core_id:
        try:
            core_uuid = uuid.UUID(str(core_id))
        except ValueError:
            return JsonResponse({'error': 'Invalid core_id UUID'}, status=400)
        ledger = SyncLedger.objects.filter(core_id=core_uuid).first()
    elif inventree_model and inventree_pk:
        ledger = SyncLedger.objects.filter(
            inventree_model=inventree_model, inventree_pk=int(inventree_pk)
        ).first()
    else:
        return JsonResponse({
            'error': 'Provide core_id or inventree_model+inventree_pk query params',
        }, status=400)

    if not ledger:
        return JsonResponse({'found': False}, status=404)

    return JsonResponse({
        'found': True,
        'core_entity_type': ledger.core_entity_type,
        'core_id': str(ledger.core_id),
        'inventree_model': ledger.inventree_model,
        'inventree_pk': ledger.inventree_pk,
        'sync_status': ledger.sync_status,
        'last_synced_at': ledger.last_synced_at.isoformat() if ledger.last_synced_at else None,
    })


@require_GET
def sync_status(request):
    """Health/status endpoint showing sync state summary."""
    now = timezone.now()

    ledger_counts = {}
    for entity_type in ['sales_order', 'job', 'inventory_item', 'warehouse', 'warehouse_location']:
        entries = SyncLedger.objects.filter(core_entity_type=entity_type)
        ledger_counts[entity_type] = {
            'total': entries.count(),
            'synced': entries.filter(sync_status='synced').count(),
            'pending': entries.filter(sync_status='pending').count(),
            'error': entries.filter(sync_status='error').count(),
        }

    return JsonResponse({
        'status': 'ok',
        'timestamp': now.isoformat(),
        'sync_ledger': ledger_counts,
    })
