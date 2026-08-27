# FireWatch as a headless service: poll the feeds, serve the map.
#
# The menu bar app is macOS-only and is not part of this image. Everything else -
# fetching, clipping, clustering, alerting, map rendering - is portable Python.
FROM python:3.14-slim

# rumps and pyobjc are deliberately absent: they are macOS-only and only menubar.py
# imports them, lazily. shapely and pyproj are here so `firewatch buffer` works in
# the image; drop them if you commit data/*-buffer.geojson and never regenerate it.
RUN pip install --no-cache-dir requests certifi shapely pyproj

WORKDIR /app
COPY firewatch/ ./firewatch/
COPY data/ ./data/

# Paths that would otherwise land under a home directory. Mount a volume on
# /var/lib/firewatch to keep history across restarts - without it, every restart
# starts from an empty database and the long map ranges are blank until backfill.
ENV FIREWATCH_DATA_DIR=/var/lib/firewatch \
    FIREWATCH_PUBLIC_DIR=/var/lib/firewatch/public \
    FIREWATCH_CONFIG_DIR=/etc/firewatch \
    PYTHONUNBUFFERED=1
VOLUME ["/var/lib/firewatch"]

# 0.0.0.0 inside the container is correct - the container boundary is the guard,
# and you publish it with -p. Put a TLS terminator in front for anything public.
EXPOSE 8080

# /health is 200 only while the snapshot is fresh, so this catches the failure that
# matters - polling stopped while the static files stayed perfectly serveable.
# start-period covers the first cycle, during which it reports "starting".
HEALTHCHECK --interval=60s --timeout=10s --start-period=300s --retries=3 \
  CMD python3 -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=8).status==200 else 1)"

# Secrets and the SMS sender come in as environment variables:
#   -e FIRMS_MAP_KEY=...  -e HTTPSMS_API_KEY=...  -e HTTPSMS_FROM=+387...
# Recipients are runtime state, not deploy config: mount /etc/firewatch and manage
# them with `docker exec <container> python3 -m firewatch sms-add +387...`, which the
# running poller picks up on its next alert without a restart.
CMD ["python3", "-m", "firewatch", "serve", "0.0.0.0:8080"]
