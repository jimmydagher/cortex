# Cortex: MCP server + web GUI for a Markdown second brain.
# Volumes: /data (the brain in /data/brain, runtime state in /data/cortex), /config (this
# environment's override file), /logs (cortex.log). Secrets arrive as files in /run/secrets.
# Nothing personal is baked in; the environment is chosen at run time with CORTEX_ENV.
# The base image is pinned to its multi-arch digest (sdsi:dependencies); keep it in step
# with scripts/python/lock.py, which resolves requirements.txt on this same image.
FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d

# Wiring only (sdsi:config): where the code and the default config live in the image, and
# where overrides are read from unless the deployment points CORTEX_OVERRIDE_DIR elsewhere.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    CORTEX_CONFIG_DIR=/app/config \
    CORTEX_OVERRIDE_DIR=/app/config/override
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --root-user-action=ignore --disable-pip-version-check -r requirements.txt

COPY VERSION ./
COPY config ./config
COPY src ./src
RUN mkdir -p /data/brain /data/cortex /config /logs && chown -R 1000:1000 /data /config /logs
USER 1000:1000

# The port inside the container (server.port in config/default.yaml); publish a different
# host port in docker-compose.yml rather than changing it.
EXPOSE 8765
VOLUME ["/data", "/config", "/logs"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"
ENTRYPOINT ["python", "-m", "cortex"]
CMD ["serve"]
