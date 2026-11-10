# syntax=docker/dockerfile:1

# ---- Build stage: turn the project into a wheel --------------------------
# Installing from a wheel (instead of copying src/) means the image gets
# exactly what a real `pip install` gets, dashboard files included.
FROM python:3.14-slim-trixie AS build

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .


# ---- Runtime stage --------------------------------------------------------
FROM python:3.14-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Inside the container the server must listen on all interfaces, or the
    # published port can't reach it. Keep it private on the host side with
    # `docker run -p 127.0.0.1:8765:8765 ...`.
    AGENT_MONITOR_HOST=0.0.0.0 \
    AGENT_MONITOR_PORT=8765 \
    AGENT_MONITOR_DB_PATH=/data/agent_monitor.db

# Run as an unprivileged user that owns only the data directory.
RUN useradd --create-home --uid 10001 monitor \
    && mkdir /data \
    && chown monitor:monitor /data

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

USER monitor
VOLUME ["/data"]
EXPOSE 8765

# The slim image has no curl, so the healthcheck uses Python itself.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2)"]

CMD ["agent-monitor"]
