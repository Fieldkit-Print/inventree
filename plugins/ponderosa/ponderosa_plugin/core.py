from django.core.exceptions import ValidationError
from plugin import InvenTreePlugin
from plugin.mixins import (
    AppMixin,
    APICallMixin,
    SettingsMixin,
    ScheduleMixin,
    EventMixin,
    BarcodeMixin,
    ReportMixin,
    UrlsMixin,
    ValidationMixin,
    UserInterfaceMixin,
    LabelPrintingMixin,
)


class PonderosaPlugin(
    AppMixin,
    APICallMixin,
    SettingsMixin,
    ScheduleMixin,
    EventMixin,
    BarcodeMixin,
    ReportMixin,
    UrlsMixin,
    ValidationMixin,
    UserInterfaceMixin,
    LabelPrintingMixin,
    InvenTreePlugin,
):
    NAME = "PonderosaPlugin"
    SLUG = "ponderosa"
    TITLE = "Ponderosa Printing Production & Inventory"
    DESCRIPTION = "Bridges Ponderosa core-app with InvenTree for production management and inventory tracking"
    VERSION = "0.1.0"
    AUTHOR = "Ponderosa Printing"

    SETTINGS = {
        'ENABLE_BARCODE_SCANNING': {
            'name': 'Enable Barcode Scanning',
            'description': 'Enable custom barcode scanning for inventory items',
            'validator': bool,
            'default': True,
        },
        'PORTAL_API_URL': {
            'name': 'Core App API URL',
            'description': 'Base URL for the Ponderosa core-app API (e.g. https://api.ponderosa.com)',
            'default': '',
        },
        'PORTAL_API_KEY': {
            'name': 'Core App API Key',
            'description': 'API key for authenticating with core-app REST API',
            'default': '',
        },
        'N8N_WEBHOOK_URL': {
            'name': 'N8N Webhook URL',
            'description': 'URL for forwarding InvenTree events to N8N (e.g. https://n8n.example.com/webhook/inventree)',
            'default': '',
        },
        'STOCK_PUSH_INTERVAL_MINUTES': {
            'name': 'Stock Push Interval (minutes)',
            'description': 'How often to push stock level snapshots to core-app',
            'default': 10,
            'validator': int,
        },
    }

    SCHEDULED_TASKS = {
        'push_stock_levels': {
            'func': 'ponderosa_plugin.scheduling.push_stock_levels',
            'schedule': 'I',
            'minutes': 10,
        },
    }

    def get_ui_panels(self, request, context, **kwargs):
        panels = []
        target_model = context.get('target_model')
        target_id = context.get('target_id')

        if target_model == 'build' and target_id:
            panels.append({
                'key': 'ponderosa-job',
                'title': 'Ponderosa Job',
                'description': 'Linked core-app job details',
                'icon': 'ti:hammer:outline',
                'feature_type': 'panel',
                'source': self.plugin_static_file('panels.js:renderJobPanel'),
            })

        if target_model == 'salesorder' and target_id:
            panels.append({
                'key': 'ponderosa-order',
                'title': 'Ponderosa Order',
                'description': 'Linked core-app sales order details',
                'icon': 'ti:clipboard-list:outline',
                'feature_type': 'panel',
                'source': self.plugin_static_file('panels.js:renderOrderPanel'),
            })

        if target_model == 'part' and target_id:
            panels.append({
                'key': 'ponderosa-inventory-sync',
                'title': 'Inventory Sync',
                'description': 'Core-app inventory sync status',
                'icon': 'ti:refresh:outline',
                'feature_type': 'panel',
                'source': self.plugin_static_file('panels.js:renderInventorySyncPanel'),
            })

        return panels

    def setup_urls(self):
        from django.urls import path
        from ponderosa_plugin.webhook_views import (
            register_sync_mapping,
            lookup_sync_mapping,
            sync_status,
        )
        from ponderosa_plugin.api_endpoints import (
            job_detail,
            order_detail,
            inventory_sync_status,
            sync_dashboard,
        )

        return [
            path('api/sync-mapping/', register_sync_mapping, name='ponderosa-sync-mapping-register'),
            path('api/sync-mapping/lookup/', lookup_sync_mapping, name='ponderosa-sync-mapping-lookup'),
            path('status/', sync_status, name='ponderosa-status'),
            path('api/job-detail/<int:build_pk>/', job_detail, name='ponderosa-job-detail'),
            path('api/order-detail/<int:so_pk>/', order_detail, name='ponderosa-order-detail'),
            path('api/inventory-sync/<int:part_pk>/', inventory_sync_status, name='ponderosa-inventory-sync'),
            path('api/sync-dashboard/', sync_dashboard, name='ponderosa-sync-dashboard'),
        ]

    def validate_model_deletion(self, instance):
        """ValidationMixin hook — prevent deletion of synced items."""
        from build.models import Build
        from order.models import SalesOrder
        from part.models import Part
        from ponderosa_plugin.models import SyncLedger

        model_map = {
            Part: 'Part',
            Build: 'Build',
            SalesOrder: 'SalesOrder',
        }

        model_name = model_map.get(type(instance))
        if not model_name:
            return

        linked = SyncLedger.objects.filter(
            inventree_model=model_name, inventree_pk=instance.pk
        ).exists()

        if linked:
            raise ValidationError(
                f"This {model_name} is synced with Ponderosa core-app and cannot be deleted. "
                f"Delete or unlink it in the core-app first."
            )

    def validate_model_instance(self, instance, deltas=None):
        """ValidationMixin hook — prevent editing synced Build Order references."""
        from build.models import Build
        from ponderosa_plugin.models import SyncLedger

        if not isinstance(instance, Build):
            return

        if not instance.pk:
            return  # New instance, not yet saved

        # Check if this Build is synced
        ledger = SyncLedger.objects.filter(
            inventree_model='Build', inventree_pk=instance.pk
        ).first()
        if not ledger:
            return

        # If deltas provided, check if reference is being changed
        if deltas and 'reference' in deltas:
            raise ValidationError(
                "The reference for this Build Order is managed by Ponderosa core-app "
                "and cannot be changed manually."
            )
