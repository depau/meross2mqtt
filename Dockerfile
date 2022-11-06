# syntax=docker.io/docker/dockerfile:1

FROM python:3.10

RUN --mount=target=/app \
    --mount=target=/root/.cache/pip,type=cache \
    cp -a /app /tmp/app && \
    cd /tmp/app && \
    pip install --compile . && \
    rm -Rf /tmp/app && \
    mkdir -p /config

WORKDIR /config
VOLUME /config

ENTRYPOINT ["python3", "-m", "meross2homie"]
