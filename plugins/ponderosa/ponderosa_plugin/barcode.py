"""BarcodeMixin — scan core-app job QR codes and inventory SKU barcodes.

Scans a core-app job UUID or jobNumber QR code and navigates to the
corresponding InvenTree Build Order. Scans an inventory SKU barcode
and navigates to the InvenTree Part/StockItem.
"""

import logging

from ponderosa_plugin.models import SyncLedger

logger = logging.getLogger('ponderosa_plugin')


def scan(plugin, barcode_data: str):
    """Handle a barcode scan event.

    Returns a dict with navigation target if matched, or None to pass
    through to other barcode handlers.
    """
    if not barcode_data:
        return None

    data = barcode_data.strip()

    # Try UUID format first (core-app Job/SalesOrder ID)
    result = _try_uuid_lookup(data)
    if result:
        return result

    # Try as a job number (e.g. "J-1234")
    result = _try_job_number_lookup(data)
    if result:
        return result

    # Try as an inventory SKU → Part IPN
    result = _try_sku_lookup(data)
    if result:
        return result

    return None


def _try_uuid_lookup(data: str):
    """Look up a UUID in the SyncLedger to find a linked InvenTree object."""
    import uuid
    try:
        parsed = uuid.UUID(data)
    except ValueError:
        return None

    ledger = SyncLedger.objects.filter(core_id=parsed).first()
    if not ledger:
        return None

    return _build_navigation_result(ledger)


def _try_job_number_lookup(data: str):
    """Look up a job number by matching Build Order reference."""
    from build.models import Build

    try:
        build = Build.objects.filter(reference=data).first()
    except Exception:
        return None

    if not build:
        return None

    return {
        'model': 'build',
        'pk': build.pk,
        'url': f'/build/{build.pk}/',
    }


def _try_sku_lookup(data: str):
    """Look up an SKU by matching Part IPN."""
    from part.models import Part

    try:
        part = Part.objects.filter(IPN=data).first()
    except Exception:
        return None

    if not part:
        return None

    return {
        'model': 'part',
        'pk': part.pk,
        'url': f'/part/{part.pk}/',
    }


def _build_navigation_result(ledger: SyncLedger) -> dict:
    """Build a navigation result dict from a SyncLedger entry."""
    model_map = {
        'Build': ('build', '/build/{pk}/'),
        'SalesOrder': ('salesorder', '/order/sales-order/{pk}/'),
        'Part': ('part', '/part/{pk}/'),
        'StockItem': ('stockitem', '/stock/item/{pk}/'),
    }

    model_info = model_map.get(ledger.inventree_model)
    if not model_info:
        return None

    model_name, url_template = model_info
    return {
        'model': model_name,
        'pk': ledger.inventree_pk,
        'url': url_template.format(pk=ledger.inventree_pk),
    }
