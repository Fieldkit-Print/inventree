"""ReportMixin — inject core-app job/client data into build order report context.

When generating reports for Build Orders or Sales Orders, this adds
Ponderosa-specific fields (client, job details, order financials).
"""

import logging

from ponderosa_plugin.models import SyncLedger

logger = logging.getLogger('ponderosa_plugin')


def add_report_context(plugin, report_instance, model_instance, request, context: dict):
    """Called by ReportMixin to add extra context to report templates.

    Detects the model type and enriches the context with core-app data
    if the instance is linked via SyncLedger.
    """
    from build.models import Build
    from order.models import SalesOrder

    if isinstance(model_instance, Build):
        _enrich_build_context(plugin, model_instance, context)
    elif isinstance(model_instance, SalesOrder):
        _enrich_sales_order_context(plugin, model_instance, context)


def _enrich_build_context(plugin, build, context: dict):
    """Add core-app Job data to a Build Order report context."""
    ledger = SyncLedger.objects.filter(
        inventree_model='Build', inventree_pk=build.pk
    ).first()

    if not ledger:
        context['ponderosa_linked'] = False
        return

    context['ponderosa_linked'] = True
    context['ponderosa_job_id'] = str(ledger.core_id)
    context['ponderosa_job_number'] = build.reference or ''

    # Sales order / client info
    if build.sales_order:
        context['ponderosa_order_number'] = build.sales_order.reference or ''
        if build.sales_order.customer:
            context['ponderosa_client_name'] = build.sales_order.customer.name or ''
        else:
            context['ponderosa_client_name'] = ''
    else:
        context['ponderosa_order_number'] = ''
        context['ponderosa_client_name'] = ''

    # Optionally fetch live data from core-app
    _try_enrich_from_api(plugin, ledger, context, 'job')


def _enrich_sales_order_context(plugin, sales_order, context: dict):
    """Add core-app SalesOrder data to an SO report context."""
    ledger = SyncLedger.objects.filter(
        inventree_model='SalesOrder', inventree_pk=sales_order.pk
    ).first()

    if not ledger:
        context['ponderosa_linked'] = False
        return

    context['ponderosa_linked'] = True
    context['ponderosa_order_id'] = str(ledger.core_id)
    context['ponderosa_order_number'] = sales_order.reference or ''

    if sales_order.customer:
        context['ponderosa_client_name'] = sales_order.customer.name or ''

    _try_enrich_from_api(plugin, ledger, context, 'sales_order')


def _try_enrich_from_api(plugin, ledger, context: dict, entity_type: str):
    """Attempt to fetch live data from core-app API for report enrichment."""
    from ponderosa_plugin.sync_engine import CoreAppClient

    base_url = plugin.get_setting('PORTAL_API_URL')
    api_key = plugin.get_setting('PORTAL_API_KEY')
    if not base_url or not api_key:
        return

    client = CoreAppClient(base_url, api_key)
    try:
        if entity_type == 'job':
            data = client.get_job(str(ledger.core_id))
            context['ponderosa_job_data'] = data
        elif entity_type == 'sales_order':
            data = client.get_sales_order(str(ledger.core_id))
            context['ponderosa_order_data'] = data
    except Exception as e:
        logger.warning("Failed to fetch %s %s for report: %s", entity_type, ledger.core_id, e)
