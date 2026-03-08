# Extend the official InvenTree stable image
FROM inventree/inventree:stable

# Install any system-level dependencies your plugins need
# USER root
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     some-system-package \
#  && rm -rf /var/lib/apt/lists/*
# USER inventree

# Copy plugin requirements and install them
COPY plugins/requirements.txt /home/inventree/plugins-requirements.txt
RUN pip install --no-cache-dir -r /home/inventree/plugins-requirements.txt || true

# Copy the custom plugin package and install in editable mode
COPY plugins/ponderosa /home/inventree/custom-plugins/ponderosa
RUN pip install --no-cache-dir -e /home/inventree/custom-plugins/ponderosa || true

# Copy additional plugins.txt for any third-party InvenTree plugins
COPY config/plugins.txt /home/inventree/data/plugins.txt
