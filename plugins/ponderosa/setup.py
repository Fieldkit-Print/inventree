from setuptools import setup, find_packages

setup(
    name='ponderosa-inventree-plugin',
    version='0.1.0',
    description='Custom InvenTree plugins for Ponderosa Printing',
    packages=find_packages(),
    install_requires=[
        'requests',
    ],
    entry_points={
        'inventree_plugins': [
            'PonderosaPlugin = ponderosa_plugin.core:PonderosaPlugin',
        ],
    },
)
