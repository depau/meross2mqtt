FROM python:3.10-alpine

RUN --mount=target=/app \
    cd /app && \
    pip install --no-cache-dir . && \
    mkdir -p /config

WORKDIR /config
VOLUME /config

ENTRYPOINT ["meross2mqtt"]