FROM docker.io/library/python:3.12.12-slim-bookworm@sha256:2986c55feb36e6cae00fa1fefb454283e4b33f35e75ff8bdd123b134130be301

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN useradd --uid 1000 --create-home apexcrew \
    && pip install --no-cache-dir .

# Executor deployments must invoke this image with --network=none.
LABEL org.apexcrew.network="none" \
      org.apexcrew.docker_socket="denied"
USER 1000:1000
ENTRYPOINT ["apexcrew"]
