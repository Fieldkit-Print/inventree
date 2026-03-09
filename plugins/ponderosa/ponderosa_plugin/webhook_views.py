"""Webhook endpoints for receiving events from core-app's InvenTreeIntegrationConnector."""

import hashlib
import hmac
import json
import logging
import uuid

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET

from plugin.registry import registry

from ponderosa_plugin.models import WebhookInbox, SyncLedger

logger = logging.getLogger('ponderosa_plugin')


def _get_plugin():
    return registry.get_plugin('ponderosa')


def _validate_hmac(request) -> bool:
    """Validate the HMAC-SHA256 signature on inbound webhooks."""
    plugin = _get_plugin()
    if not plugin:
        return False

    secret = plugin.get_setting('PORTAL_WEBHOOK_SECRET')
    if not secret:
        # No secret configured — accept all (development mode)
        logger.warning("No PORTAL_WEBHOOK_SECRET configured — skipping HMAC validation")
        return True

    signature = request.headers.get('X-Ponderosa-Signature', '')
    if not signature:
        return False

    expected = hmac.new(
        secret.encode('utf-8'),
        request.body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(signature, expected)


@csrf_exempt
@require_POST
def webhook_receive(request):
    """Receive events from core-app, validate HMAC, buffer in WebhookInbox.

    Returns 202 Accepted on success. The inbox is processed asynchronously
    by the scheduled task `process_webhook_inbox`.
    """
    if not _validate_hmac(request):
        return JsonResponse({'error': 'Invalid signature'}, status=401)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    # Extract event metadata
    event_id = body.get('eventId')
    if not event_id:
        return JsonResponse({'error': 'Missing eventId'}, status=400)

    try:
        event_uuid = uuid.UUID(str(event_id))
    except ValueError:
        return JsonResponse({'error': 'Invalid eventId format'}, status=400)

    resource_type = body.get('resourceType', '')
    action = body.get('action', '')
    event_type = f"{resource_type}.{action}"

    # Idempotency: skip if we already have this event
    if WebhookInbox.objects.filter(event_id=event_uuid).exists():
        logger.debug("Duplicate webhook event %s — returning 202", event_id)
        return JsonResponse({'status': 'already_received'}, status=202)

    WebhookInbox.objects.create(
        event_id=event_uuid,
        event_type=event_type,
        payload=body,
    )

    logger.info("Received webhook event %s: %s", event_id, event_type)
    return JsonResponse({'status': 'accepted'}, status=202)


@require_GET
def sync_status(request):
    """Health/status endpoint showing sync state summary."""
    now = timezone.now()

    inbox_pending = WebhookInbox.objects.filter(processed_at__isnull=True).count()
    inbox_errored = WebhookInbox.objects.filter(
        processed_at__isnull=True, attempts__gte=3
    ).count()

    ledger_counts = {}
    for entity_type in ['sales_order', 'job', 'inventory_item']:
        counts = {
            'total': SyncLedger.objects.filter(core_entity_type=entity_type).count(),
            'synced': SyncLedger.objects.filter(core_entity_type=entity_type, sync_status='synced').count(),
            'pending': SyncLedger.objects.filter(core_entity_type=entity_type, sync_status='pending').count(),
            'error': SyncLedger.objects.filter(core_entity_type=entity_type, sync_status='error').count(),
        }
        ledger_counts[entity_type] = counts

    return JsonResponse({
        'status': 'ok',
        'timestamp': now.isoformat(),
        'inbox': {
            'pending': inbox_pending,
            'errored': inbox_errored,
        },
        'sync_ledger': ledger_counts,
    })
