"""EventMixin handlers — reacts to InvenTree-native events.

Forwards relevant events to:
1. Core-app: build.completed/cancelled → push job status
2. N8N webhook: all relevant events for workflow automation
3. Auto-generates production steps on build creation
"""

import logging

import requests
from plugin.registry import registry

from ponderosa_plugin.models import SyncLedger
from ponderosa_plugin.sync_engine import BUILD_STATUS_TO_JOB

logger = logging.getLogger('ponderosa_plugin')

# Events we forward to N8N
N8N_FORWARDED_EVENTS = {
    'build.completed',
    'build.cancelled',
    'build.created',
    'build.saved',
    'build.deleted',
    'salesorder.created',
    'salesorder.saved',
    'salesorder.deleted',
    'stockitem.created',
    'stockitem.saved',
    'stockitem.deleted',
    'stocklocation.created',
    'stocklocation.saved',
    'part.created',
    'part.saved',
}


def process_event(plugin, event: str, **kwargs):
    """Called by EventMixin when an InvenTree event fires."""
    # Auto-generate production steps for new builds
    if event == 'build.created':
        _handle_build_created(plugin, kwargs.get('id'))

    # Push terminal build statuses back to core-app
    if event == 'build.completed':
        _handle_build_status_change(plugin, kwargs.get('id'), 30)
    elif event == 'build.cancelled':
        _handle_build_status_change(plugin, kwargs.get('id'), 40)
        _handle_build_cancelled_steps(kwargs.get('id'))

    # Forward to N8N webhook
    if event in N8N_FORWARDED_EVENTS:
        _forward_to_n8n(plugin, event, kwargs)


def _handle_build_created(plugin, build_pk: int | None):
    """Auto-generate BuildOrderSteps from the Part's ProductionStepTemplates."""
    if build_pk is None:
        return

    if not plugin.get_setting('AUTO_CREATE_BUILD_STEPS'):
        return

    from build.models import Build
    from ponderosa_plugin.models import ProductionStepTemplate, BuildOrderStep

    try:
        build = Build.objects.get(pk=build_pk)
    except Build.DoesNotExist:
        return

    # Skip if steps already exist (idempotency)
    if BuildOrderStep.objects.filter(build=build).exists():
        return

    templates = ProductionStepTemplate.objects.filter(part=build.part).order_by('sequence')
    if not templates.exists():
        logger.debug("Build %s part has no step templates — skipping auto-generation", build_pk)
        return

    steps_created = []
    for i, tmpl in enumerate(templates):
        step = BuildOrderStep.objects.create(
            build=build,
            template=tmpl,
            sequence=tmpl.sequence,
            step_type=tmpl.step_type,
            name=tmpl.name,
            status='queued' if i == 0 else 'pending',
            metadata=tmpl.metadata,
        )
        steps_created.append(step)

    logger.info("Auto-created %d production steps for Build %s", len(steps_created), build_pk)


def _handle_build_cancelled_steps(build_pk: int | None):
    """Mark all non-terminal steps as skipped when a build is cancelled."""
    if build_pk is None:
        return

    from ponderosa_plugin.models import BuildOrderStep

    updated = BuildOrderStep.objects.filter(
        build_id=build_pk,
        status__in=['pending', 'queued', 'in_progress', 'on_hold', 'blocked'],
    ).update(status='skipped')

    if updated:
        logger.info("Skipped %d remaining steps for cancelled Build %s", updated, build_pk)


def _handle_build_status_change(plugin, build_pk: int | None, status_code: int):
    """Push a build status change back to core-app."""
    if build_pk is None:
        return

    ledger = SyncLedger.objects.filter(
        inventree_model='Build', inventree_pk=build_pk
    ).first()
    if not ledger:
        logger.debug("Build %s has no SyncLedger entry — not a synced job", build_pk)
        return

    job_status = BUILD_STATUS_TO_JOB.get(status_code)
    if not job_status:
        return

    base_url = plugin.get_setting('PORTAL_API_URL')
    api_key = plugin.get_setting('PORTAL_API_KEY')
    if not base_url or not api_key:
        logger.warning("Core-app API not configured — cannot push build status")
        return

    from ponderosa_plugin.sync_engine import CoreAppClient

    client = CoreAppClient(base_url, api_key)
    try:
        client.push_job_status(str(ledger.core_id), job_status)
        logger.info(
            "Pushed status %s to core-app job %s (Build %s)",
            job_status, ledger.core_id, build_pk,
        )
    except Exception as e:
        logger.exception("Failed to push build status to core-app: %s", e)


def _forward_to_n8n(plugin, event: str, kwargs: dict):
    """Forward an InvenTree event to the configured N8N webhook URL."""
    n8n_url = plugin.get_setting('N8N_WEBHOOK_URL')
    if not n8n_url:
        return

    # Build payload with event info and any SyncLedger context
    payload = {
        'source': 'inventree',
        'event': event,
        'model': kwargs.get('model', None),
        'id': kwargs.get('id', None),
        'sender': str(kwargs.get('sender', '')),
    }

    # Enrich with core-app ID if this entity is synced
    instance_pk = kwargs.get('id')
    if instance_pk:
        model_name = _event_to_model_name(event)
        if model_name:
            ledger = SyncLedger.objects.filter(
                inventree_model=model_name, inventree_pk=instance_pk
            ).first()
            if ledger:
                payload['core_id'] = str(ledger.core_id)
                payload['core_entity_type'] = ledger.core_entity_type

    try:
        requests.post(n8n_url, json=payload, timeout=10)
        logger.debug("Forwarded event %s to N8N", event)
    except Exception as e:
        logger.warning("Failed to forward event %s to N8N: %s", event, e)


def _event_to_model_name(event: str) -> str | None:
    """Map an InvenTree event prefix to a SyncLedger inventree_model name."""
    prefix = event.split('.')[0] if '.' in event else event
    return {
        'build': 'Build',
        'salesorder': 'SalesOrder',
        'stockitem': 'StockItem',
        'stocklocation': 'StockLocation',
        'part': 'Part',
    }.get(prefix)


def forward_event_to_n8n(payload: dict):
    """Public function to forward an arbitrary payload to the N8N webhook.

    Used by production_api.py to send step transition events.
    """
    plugin = registry.get_plugin('ponderosa')
    if not plugin:
        return

    n8n_url = plugin.get_setting('N8N_WEBHOOK_URL')
    if not n8n_url:
        return

    try:
        requests.post(n8n_url, json=payload, timeout=10)
        logger.debug("Forwarded custom event to N8N: %s", payload.get('event', 'unknown'))
    except Exception as e:
        logger.warning("Failed to forward event to N8N: %s", e)
