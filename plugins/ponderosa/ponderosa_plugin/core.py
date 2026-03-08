from plugin import InvenTreePlugin
from plugin.mixins import (
    SettingsMixin,
    ScheduleMixin,
    EventMixin,
    BarcodeMixin,
    ReportMixin,
    UserInterfaceMixin,
    LabelPrintingMixin,
)


class PonderosaPlugin(
    SettingsMixin,
    ScheduleMixin,
    EventMixin,
    BarcodeMixin,
    ReportMixin,
    UserInterfaceMixin,
    LabelPrintingMixin,
    InvenTreePlugin,
):
    NAME = "PonderosaPlugin"
    SLUG = "ponderosa"
    TITLE = "Ponderosa Printing Inventory"
    DESCRIPTION = "Custom inventory extensions for Ponderosa Printing"
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
            'name': 'Production Portal API URL',
            'description': 'Base URL for the Ponderosa production portal API',
            'default': '',
        },
        'PORTAL_API_KEY': {
            'name': 'Portal API Key',
            'description': 'API key for production portal integration',
            'default': '',
            'required': False,
        },
    }
