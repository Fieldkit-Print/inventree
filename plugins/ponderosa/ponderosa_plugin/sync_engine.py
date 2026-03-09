"""Core sync logic: HTTP client for core-app and handlers for each entity type."""

import logging
import time
from datetime import datetime

import requests

from build.models import Build
from company.models import Company
from order.models import SalesOrder, SalesOrderLineItem
from part.models import Part, PartCategory

from ponderosa_plugin.models import SyncLedger

logger = logging.getLogger('ponderosa_plugin')

# ---------------------------------------------------------------------------
# Status mapping tables
# ---------------------------------------------------------------------------

# Core-app JobStatus → InvenTree Build status code
JOB_STATUS_TO_BUILD = {
    'PENDING': 10,
    'AWAITING_PROOFS': 10,
    'AWAITING_PROOF_APPROVAL': 10,
    'AWAITING_PRODUCTION_FILES': 10,
    'AWAITING_GOODS': 10,
    'AWAITING_PRODUCTION': 10,
    'IN_PRODUCTION': 20,
    'COMPLETE': 30,
    'CANCELLED': 40,
}

# InvenTree Build status code → core-app JobStatus to push back
BUILD_STATUS_TO_JOB = {
    10: None,       # PENDING — no pushback, core-app drives pre-production states
    20: None,       # PRODUCTION — already set when job was pushed
    30: 'COMPLETE',
    40: 'CANCELLED',
}

# Core-app SalesOrderStatus → InvenTree SO status code
SO_STATUS_TO_INVENTREE = {
    'DRAFT': 10,
    'PENDING': 10,
    'NEEDS_CUSTOMER_INFO': 10,
    'QUOTING': 10,
    'AWAITING_QUOTE_APPROVAL': 10,
    'AWAITING_PAYMENT_SOW': 10,
    'READY': 15,
    'IN_PRODUCTION': 15,
    'PRODUCTION_COMPLETE': 15,
    'SHIPPED': 20,
    'CLOSEOUT': 30,
    'COMPLETE': 30,
    'CANCELLED': 40,
}


class CoreAppClient:
    """HTTP client wrapping calls to the Ponderosa core-app REST API."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({
            'X-API-Key': api_key,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        })
        self.timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v1{path}"

    MAX_RETRIES = 3
    BACKOFF_BASE = 2  # seconds

    def _request(self, method: str, path: str, **kwargs):
        """Make an HTTP request with exponential backoff on transient failures."""
        url = self._url(path)
        last_exc = None
        for attempt in range(self.MAX_RETRIES):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
                if resp.status_code >= 500:
                    raise requests.HTTPError(
                        f"Server error {resp.status_code}", response=resp
                    )
                resp.raise_for_status()
                return resp
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
                last_exc = e
                if attempt < self.MAX_RETRIES - 1:
                    wait = self.BACKOFF_BASE ** (attempt + 1)
                    logger.warning(
                        "Request to %s failed (attempt %d/%d), retrying in %ds: %s",
                        url, attempt + 1, self.MAX_RETRIES, wait, e,
                    )
                    time.sleep(wait)
        raise last_exc

    def _get(self, path: str, params: dict | None = None):
        resp = self._request('GET', path, params=params)
        return resp.json()

    def _put(self, path: str, json_body: dict):
        resp = self._request('PUT', path, json=json_body)
        return resp.json() if resp.content else None

    # -- Read endpoints --

    def get_job(self, job_id: str) -> dict:
        return self._get(f"/jobs/{job_id}")

    def get_sales_order(self, order_id: str) -> dict:
        return self._get(f"/sales-orders/{order_id}")

    def get_job_line_items(self, job_id: str) -> list[dict]:
        return self._get(f"/jobs/{job_id}/line-items")

    def get_inventory_items(self, params: dict | None = None) -> list[dict]:
        return self._get("/inventory/items", params=params)

    # -- Write-back endpoints (InvenTree → core-app) --

    def push_job_status(self, job_id: str, status: str):
        """Push a status update back to core-app for a Job."""
        return self._put(
            f"/integrations/inventree/jobs/{job_id}/status",
            {'status': status},
        )

    def push_stock_level(self, inventory_item_id: str, quantity: int):
        """Push current stock level to core-app (read-only mirror)."""
        return self._put(
            f"/integrations/inventree/inventory/{inventory_item_id}/stock",
            {'quantity': quantity},
        )


class SalesOrderSyncHandler:
    """Creates/updates InvenTree SalesOrders as stubs from core-app data."""

    @staticmethod
    def sync(payload: dict, client: CoreAppClient | None = None):
        """Create or update an InvenTree SalesOrder from a core-app event payload.

        The payload comes from the webhook and contains the core-app resource ID.
        We fetch full details from core-app if a client is provided, otherwise
        use the attributes embedded in the event.
        """
        resource_id = payload.get('resourceId')
        attrs = payload.get('attributes', {})

        order_number = attrs.get('orderNumber', '')
        client_name = attrs.get('clientName', '')
        status_str = attrs.get('status', 'DRAFT')
        target_date = attrs.get('requestedShipDate')

        # Find or create the customer Company
        customer = None
        if client_name:
            customer, _ = Company.objects.get_or_create(
                name=client_name,
                defaults={'is_customer': True, 'is_supplier': False},
            )

        inventree_status = SO_STATUS_TO_INVENTREE.get(status_str, 10)

        # Check SyncLedger for existing mapping
        ledger = SyncLedger.objects.filter(
            core_entity_type='sales_order', core_id=resource_id
        ).first()

        if ledger:
            try:
                so = SalesOrder.objects.get(pk=ledger.inventree_pk)
                so.status = inventree_status
                if customer:
                    so.customer = customer
                if target_date:
                    so.target_date = target_date
                so.save()
                ledger.sync_status = 'synced'
                ledger.error_message = None
                ledger.save()
                logger.info("Updated InvenTree SO %s for core-app %s", so.pk, resource_id)
                return so
            except SalesOrder.DoesNotExist:
                ledger.delete()

        # Create new stub Sales Order
        so = SalesOrder.objects.create(
            reference=order_number,
            customer=customer,
            status=inventree_status,
            target_date=target_date,
            description=f"Synced from Ponderosa core-app order {order_number}",
        )
        SyncLedger.objects.create(
            core_entity_type='sales_order',
            core_id=resource_id,
            inventree_model='SalesOrder',
            inventree_pk=so.pk,
            sync_status='synced',
        )
        logger.info("Created InvenTree SO %s for core-app %s", so.pk, resource_id)
        return so

    @staticmethod
    def update_status(payload: dict):
        """Update only the status of an existing synced SalesOrder."""
        resource_id = payload.get('resourceId')
        attrs = payload.get('attributes', {})
        status_str = attrs.get('status', '')

        ledger = SyncLedger.objects.filter(
            core_entity_type='sales_order', core_id=resource_id
        ).first()
        if not ledger:
            logger.warning("No SyncLedger entry for SO %s — skipping status update", resource_id)
            return

        inventree_status = SO_STATUS_TO_INVENTREE.get(status_str)
        if inventree_status is None:
            logger.warning("Unknown SO status '%s' for %s", status_str, resource_id)
            return

        try:
            so = SalesOrder.objects.get(pk=ledger.inventree_pk)
            so.status = inventree_status
            so.save()
            ledger.sync_status = 'synced'
            ledger.save()
        except SalesOrder.DoesNotExist:
            ledger.sync_status = 'error'
            ledger.error_message = 'InvenTree SalesOrder not found'
            ledger.save()


class BuildOrderSyncHandler:
    """Creates/updates InvenTree Build Orders from core-app Jobs.

    Jobs are only pushed to InvenTree when they're ready for production
    (proof approved + paid/SOW signed). The core-app connector controls
    when events are sent.
    """

    @staticmethod
    def sync(payload: dict, client: CoreAppClient | None = None):
        """Create or update an InvenTree Build Order from a core-app Job event."""
        resource_id = payload.get('resourceId')
        attrs = payload.get('attributes', {})

        job_number = attrs.get('jobNumber', '')
        job_name = attrs.get('name', '')
        quantity = attrs.get('quantity', 1)
        status_str = attrs.get('status', 'PENDING')
        due_date = attrs.get('dueDate')
        sales_order_id = attrs.get('salesOrderId')

        inventree_status = JOB_STATUS_TO_BUILD.get(status_str, 10)

        # Look up linked SalesOrder in InvenTree (if we synced it)
        sales_order = None
        if sales_order_id:
            so_ledger = SyncLedger.objects.filter(
                core_entity_type='sales_order', core_id=sales_order_id
            ).first()
            if so_ledger:
                try:
                    sales_order = SalesOrder.objects.get(pk=so_ledger.inventree_pk)
                except SalesOrder.DoesNotExist:
                    pass

        # Check SyncLedger for existing mapping
        ledger = SyncLedger.objects.filter(
            core_entity_type='job', core_id=resource_id
        ).first()

        if ledger:
            try:
                build = Build.objects.get(pk=ledger.inventree_pk)
                build.status = inventree_status
                build.quantity = quantity
                if due_date:
                    build.target_date = due_date
                if sales_order:
                    build.sales_order = sales_order
                build.save()
                ledger.sync_status = 'synced'
                ledger.error_message = None
                ledger.save()
                logger.info("Updated InvenTree Build %s for job %s", build.pk, resource_id)
                return build
            except Build.DoesNotExist:
                ledger.delete()

        # We need a Part to create a Build Order. Use a generic "Production Job" part
        # or look up by job attributes. For now, use a placeholder assembly part.
        part = BuildOrderSyncHandler._resolve_or_create_part(attrs)

        build = Build.objects.create(
            reference=job_number,
            title=job_name or f"Job {job_number}",
            part=part,
            quantity=quantity,
            status=inventree_status,
            target_date=due_date,
            sales_order=sales_order,
        )
        SyncLedger.objects.create(
            core_entity_type='job',
            core_id=resource_id,
            inventree_model='Build',
            inventree_pk=build.pk,
            sync_status='synced',
        )
        logger.info("Created InvenTree Build %s for job %s (%s)", build.pk, resource_id, job_number)
        return build

    @staticmethod
    def update_status(payload: dict):
        """Update only the status of an existing synced Build Order."""
        resource_id = payload.get('resourceId')
        attrs = payload.get('attributes', {})
        status_str = attrs.get('status', '')

        ledger = SyncLedger.objects.filter(
            core_entity_type='job', core_id=resource_id
        ).first()
        if not ledger:
            logger.warning("No SyncLedger entry for Job %s — skipping status update", resource_id)
            return

        inventree_status = JOB_STATUS_TO_BUILD.get(status_str)
        if inventree_status is None:
            logger.warning("Unknown Job status '%s' for %s", status_str, resource_id)
            return

        try:
            build = Build.objects.get(pk=ledger.inventree_pk)
            build.status = inventree_status
            build.save()
            ledger.sync_status = 'synced'
            ledger.save()
        except Build.DoesNotExist:
            ledger.sync_status = 'error'
            ledger.error_message = 'InvenTree Build not found'
            ledger.save()

    @staticmethod
    def _resolve_or_create_part(attrs: dict) -> Part:
        """Resolve or create a Part for the build order.

        Uses job line item info if available, otherwise creates/returns
        a generic assembly part for production jobs.
        """
        category, _ = PartCategory.objects.get_or_create(
            name='Production Jobs',
            defaults={'description': 'Parts representing production job outputs'},
        )
        # Use a generic assembly part — individual BOM items will be linked in Phase 3
        part, _ = Part.objects.get_or_create(
            name='Production Job Assembly',
            defaults={
                'description': 'Generic assembly part for synced production jobs',
                'category': category,
                'assembly': True,
                'component': False,
                'trackable': False,
                'active': True,
            },
        )
        return part


class PartSyncHandler:
    """Ensures InvenTree Parts exist for core-app InventoryItems."""

    @staticmethod
    def sync(payload: dict):
        """Create or update an InvenTree Part from a core-app InventoryItem."""
        resource_id = payload.get('resourceId')
        attrs = payload.get('attributes', {})

        sku = attrs.get('sku', '')
        name = attrs.get('name', 'Unknown Item')
        description = attrs.get('description', '')
        item_class = attrs.get('itemClass', 'OTHER')
        category_name = attrs.get('category', 'Uncategorized')

        category, _ = PartCategory.objects.get_or_create(
            name=category_name,
            defaults={'description': f'Auto-created from core-app category: {category_name}'},
        )

        # Determine Part flags from itemClass
        is_component = item_class in ('MATERIAL', 'PACKAGING')
        is_assembly = item_class == 'FINISHED_GOOD'

        ledger = SyncLedger.objects.filter(
            core_entity_type='inventory_item', core_id=resource_id
        ).first()

        if ledger:
            try:
                part = Part.objects.get(pk=ledger.inventree_pk)
                part.name = name
                part.description = description
                part.IPN = sku
                part.category = category
                part.component = is_component
                part.assembly = is_assembly
                part.save()
                ledger.sync_status = 'synced'
                ledger.error_message = None
                ledger.save()
                return part
            except Part.DoesNotExist:
                ledger.delete()

        part = Part.objects.create(
            name=name,
            description=description,
            IPN=sku,
            category=category,
            component=is_component,
            assembly=is_assembly,
            trackable=True,
            active=True,
        )
        SyncLedger.objects.create(
            core_entity_type='inventory_item',
            core_id=resource_id,
            inventree_model='Part',
            inventree_pk=part.pk,
            sync_status='synced',
        )
        logger.info("Created InvenTree Part %s (IPN=%s) for core-app item %s", part.pk, sku, resource_id)
        return part


class ShipmentSyncHandler:
    """Updates the linked InvenTree SalesOrder when a shipment event arrives from core-app."""

    @staticmethod
    def sync(payload: dict):
        """Process a SHIPMENT.UPSERT event.

        When core-app ships an order, we update the linked InvenTree SalesOrder
        status to SHIPPED (20) if the shipment status indicates it's with carrier.
        """
        resource_id = payload.get('resourceId')
        attrs = payload.get('attributes', {})

        sales_order_id = attrs.get('salesOrderId')
        status_str = attrs.get('status', '')
        tracking_number = attrs.get('trackingNumber', '')

        if not sales_order_id:
            logger.warning("SHIPMENT.UPSERT missing salesOrderId for shipment %s", resource_id)
            return

        # Find the linked InvenTree SalesOrder
        ledger = SyncLedger.objects.filter(
            core_entity_type='sales_order', core_id=sales_order_id
        ).first()
        if not ledger:
            logger.info("No synced SalesOrder for core-app SO %s — skipping shipment", sales_order_id)
            return

        try:
            so = SalesOrder.objects.get(pk=ledger.inventree_pk)
        except SalesOrder.DoesNotExist:
            logger.warning("InvenTree SalesOrder %s not found for shipment sync", ledger.inventree_pk)
            return

        # Map shipment status to SO status update
        shipped_statuses = {'WITH_CARRIER', 'IN_TRANSIT', 'OUT_FOR_DELIVERY', 'DELIVERED'}
        if status_str in shipped_statuses and so.status < 20:
            so.status = 20  # SHIPPED
            so.save()
            logger.info(
                "Updated InvenTree SO %s to SHIPPED from shipment %s (tracking=%s)",
                so.pk, resource_id, tracking_number,
            )

        if status_str == 'DELIVERED' and so.status < 30:
            so.status = 30  # COMPLETE
            so.save()
            logger.info("Updated InvenTree SO %s to COMPLETE — shipment %s delivered", so.pk, resource_id)


class WarehouseSyncHandler:
    """Maps core-app Warehouses and WarehouseLocations to InvenTree StockLocations."""

    @staticmethod
    def sync_warehouse(warehouse_data: dict):
        """Create or update an InvenTree StockLocation from a core-app Warehouse."""
        from stock.models import StockLocation

        warehouse_id = warehouse_data.get('id')
        name = warehouse_data.get('name', 'Unknown Warehouse')
        code = warehouse_data.get('code', '')

        ledger = SyncLedger.objects.filter(
            core_entity_type='warehouse', core_id=warehouse_id
        ).first()

        if ledger:
            try:
                loc = StockLocation.objects.get(pk=ledger.inventree_pk)
                loc.name = name
                loc.description = f"Warehouse {code}" if code else ''
                loc.save()
                ledger.sync_status = 'synced'
                ledger.error_message = None
                ledger.save()
                return loc
            except StockLocation.DoesNotExist:
                ledger.delete()

        loc = StockLocation.objects.create(
            name=name,
            description=f"Warehouse {code}" if code else '',
        )
        SyncLedger.objects.create(
            core_entity_type='warehouse',
            core_id=warehouse_id,
            inventree_model='StockLocation',
            inventree_pk=loc.pk,
            sync_status='synced',
        )
        logger.info("Created StockLocation %s for warehouse %s", loc.pk, warehouse_id)
        return loc

    @staticmethod
    def sync_location(location_data: dict, parent_location_pk: int | None = None):
        """Create or update a child StockLocation from a core-app WarehouseLocation."""
        from stock.models import StockLocation

        location_id = location_data.get('id')
        name = location_data.get('name', '')
        code = location_data.get('code', '')

        ledger = SyncLedger.objects.filter(
            core_entity_type='warehouse_location', core_id=location_id
        ).first()

        parent = None
        if parent_location_pk:
            try:
                parent = StockLocation.objects.get(pk=parent_location_pk)
            except StockLocation.DoesNotExist:
                pass

        if ledger:
            try:
                loc = StockLocation.objects.get(pk=ledger.inventree_pk)
                loc.name = name or code
                loc.parent = parent
                loc.save()
                ledger.sync_status = 'synced'
                ledger.save()
                return loc
            except StockLocation.DoesNotExist:
                ledger.delete()

        loc = StockLocation.objects.create(
            name=name or code,
            description=f"Location {code}" if code else '',
            parent=parent,
        )
        SyncLedger.objects.create(
            core_entity_type='warehouse_location',
            core_id=location_id,
            inventree_model='StockLocation',
            inventree_pk=loc.pk,
            sync_status='synced',
        )
        logger.info("Created StockLocation %s for warehouse location %s", loc.pk, location_id)
        return loc


class InitialImportHandler:
    """One-time bulk import of core-app data into InvenTree.

    Fetches all inventory items and warehouses from core-app and creates
    corresponding InvenTree Parts, PartCategories, and StockLocations.
    """

    @staticmethod
    def run(client: CoreAppClient):
        """Execute the full initial import."""
        logger.info("Starting initial import from core-app...")

        # Import warehouses first (so Parts can reference stock locations)
        imported_warehouses = InitialImportHandler._import_warehouses(client)

        # Import inventory items as Parts
        imported_parts = InitialImportHandler._import_inventory_items(client)

        logger.info(
            "Initial import complete: %d warehouses, %d parts",
            imported_warehouses, imported_parts,
        )
        return {'warehouses': imported_warehouses, 'parts': imported_parts}

    @staticmethod
    def _import_warehouses(client: CoreAppClient) -> int:
        """Fetch all warehouses and locations from core-app and create StockLocations."""
        count = 0
        try:
            warehouses = client._get('/warehouses')
            if not isinstance(warehouses, list):
                warehouses = warehouses.get('content', []) if isinstance(warehouses, dict) else []

            for wh in warehouses:
                try:
                    parent_loc = WarehouseSyncHandler.sync_warehouse(wh)
                    count += 1

                    # Sync child locations if present
                    locations = wh.get('locations', [])
                    for loc_data in locations:
                        try:
                            WarehouseSyncHandler.sync_location(loc_data, parent_loc.pk)
                            count += 1
                        except Exception as e:
                            logger.warning("Failed to import location %s: %s", loc_data.get('id'), e)
                except Exception as e:
                    logger.warning("Failed to import warehouse %s: %s", wh.get('id'), e)
        except Exception as e:
            logger.warning("Failed to fetch warehouses from core-app: %s", e)

        return count

    @staticmethod
    def _import_inventory_items(client: CoreAppClient) -> int:
        """Fetch all inventory items from core-app and create InvenTree Parts."""
        count = 0
        try:
            items = client.get_inventory_items()
            if not isinstance(items, list):
                items = items.get('content', []) if isinstance(items, dict) else []

            for item in items:
                item_id = item.get('id')
                # Skip if already synced
                if SyncLedger.objects.filter(
                    core_entity_type='inventory_item', core_id=item_id
                ).exists():
                    continue

                try:
                    payload = {
                        'resourceId': item_id,
                        'attributes': item,
                    }
                    PartSyncHandler.sync(payload)
                    count += 1
                except Exception as e:
                    logger.warning("Failed to import inventory item %s: %s", item_id, e)
        except Exception as e:
            logger.warning("Failed to fetch inventory items from core-app: %s", e)

        return count
