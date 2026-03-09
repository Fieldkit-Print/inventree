"""EventMixin handlers — reacts to InvenTree-native events.

When a Build Order is completed or cancelled in InvenTree (by production workers),
we push the status change back to core-app so the Job lifecycle stays in sync.
"""

import logging

from plugin.registry import registry

from ponderosa_plugin.models import SyncLedger
from ponderosa_plugin.sync_engine import BUILD_STATUS_TO_JOB

logger = logging.getLogger('ponderosa_plugin')


def process_event(plugin, event: str, **kwargs):
    """Called by EventMixin when an InvenTree event fires.

    We only care about build order status changes that need to be
    pushed back to core-app.
    """
    if event == 'build.completed':
        _handle_build_status_change(plugin, kwargs.get('id'), 30)
    elif event == 'build.cancelled':
        _handle_build_status_change(plugin, kwargs.get('id'), 40)


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
