# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM node:22.22.0-bookworm-slim@sha256:dd9d21971ec4395903fa6143c2b9267d048ae01ca6d3ea96f16cb30df6187d94 AS web-builder

ENV PNPM_HOME=/pnpm
ENV PATH=$PNPM_HOME:$PATH
WORKDIR /build/web

RUN corepack enable && corepack install --global pnpm@10.28.2
COPY web/package.json web/pnpm-lock.yaml ./
RUN --mount=type=cache,id=pnpm,target=/pnpm/store pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build


FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db AS python-builder

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /build/backend

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --disable-pip-version-check "uv==0.9.11"
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
COPY backend/src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db AS runtime

ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.title="Katılım Analiz" \
      org.opencontainers.image.description="Evidence-backed participation-bank campaign analysis" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.revision=$VCS_REF \
      org.opencontainers.image.created=$BUILD_DATE

ENV PATH=/app/.venv/bin:$PATH
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent app && \
    mkdir -p /app/backend /app/web /app/data/registry /app/data/private /app/datasets/demo && \
    chown -R 10001:10001 /app

COPY --from=python-builder --chown=10001:10001 /app/.venv /app/.venv
COPY --chown=10001:10001 backend/alembic.ini /app/backend/alembic.ini
COPY --chown=10001:10001 backend/migrations /app/backend/migrations
COPY --from=web-builder --chown=10001:10001 /build/web/dist /app/web/dist
COPY --chown=10001:10001 data/registry /app/data/registry
COPY --chown=10001:10001 datasets/demo /app/datasets/demo

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "katilim_analiz.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--limit-concurrency", "32"]
