"""LabelPrintingMixin — custom label templates for synced items.

Provides label data for stock items (SKU, name, location, QR) and
build orders (jobNumber, client, order, due date, QR).
"""

import logging

from ponderosa_plugin.models import SyncLedger

logger = logging.getLogger('ponderosa_plugin')


def get_stock_item_label_context(plugin, stock_item) -> dict:
    """Build extra label context for a stock item.

    Adds core-app metadata (SKU, category, client) if the stock item's
    Part is linked via SyncLedger.
    """
    context = {}

    if not stock_item or not stock_item.part:
        return context

    part = stock_item.part
    context['sku'] = part.IPN or ''
    context['part_name'] = part.name or ''
    context['location_name'] = ''
    if stock_item.location:
        context['location_name'] = stock_item.location.name or ''

    # Look up core-app metadata
    ledger = SyncLedger.objects.filter(
        inventree_model='Part', inventree_pk=part.pk
    ).first()
    if ledger:
        context['core_app_id'] = str(ledger.core_id)
        context['qr_data'] = str(ledger.core_id)
    else:
        context['qr_data'] = part.IPN or str(part.pk)

    return context


def get_build_order_label_context(plugin, build_order) -> dict:
    """Build extra label context for a build order.

    Adds core-app job metadata (jobNumber, client, order info) if the
    build is linked via SyncLedger.
    """
    context = {
        'build_reference': build_order.reference or '',
        'build_title': build_order.title or '',
        'build_quantity': build_order.quantity,
        'target_date': str(build_order.target_date) if build_order.target_date else '',
    }

    # Sales order info
    if build_order.sales_order:
        context['sales_order_reference'] = build_order.sales_order.reference or ''
        if build_order.sales_order.customer:
            context['client_name'] = build_order.sales_order.customer.name or ''
    else:
        context['sales_order_reference'] = ''
        context['client_name'] = ''

    # Core-app link
    ledger = SyncLedger.objects.filter(
        inventree_model='Build', inventree_pk=build_order.pk
    ).first()
    if ledger:
        context['core_app_job_id'] = str(ledger.core_id)
        context['qr_data'] = str(ledger.core_id)
    else:
        context['qr_data'] = build_order.reference or str(build_order.pk)

    return context
