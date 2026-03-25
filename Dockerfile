ARG INVENTREE_TAG=stable

# prebuild stage — compile CUPS plugin (needs build deps)
FROM python:3.11-slim-trixie AS prebuild

RUN apt-get update && apt-get install -y libcups2-dev gcc git musl-dev && apt-get clean && \
    pip install --user --no-cache-dir git+https://github.com/wolflu05/inventree-cups-plugin

# production image — only install the CUPS shared library
FROM inventree/inventree:${INVENTREE_TAG} AS production

RUN apt-get update && apt-get install -y libcups2 && apt-get clean
COPY --from=prebuild /root/.local /root/.local

# Install Zebra ZPL label printing plugin
RUN pip install --no-cache-dir inventree-zebra-plugin

# Copy plugin requirements and install them
COPY plugins/requirements.txt /home/inventree/plugins-requirements.txt
RUN pip install --no-cache-dir -r /home/inventree/plugins-requirements.txt || true

# Copy the custom plugin package and install in editable mode
COPY plugins/ponderosa /home/inventree/custom-plugins/ponderosa
RUN pip install --no-cache-dir -e /home/inventree/custom-plugins/ponderosa || true

# Copy additional plugins.txt for any third-party InvenTree plugins
COPY config/plugins.txt /home/inventree/data/plugins.txt
