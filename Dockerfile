# syntax=docker/dockerfile:1
# The tool-approvals image: one console script (`tool-approvals call-gate | approval-wait | stub-tool | approvals ...`),
# no secrets, uid 65532. Bytecode is compiled at build time, so no command writes to the (read-only) root.
# Base images are pinned by digest and named in each FROM, where Dependabot finds and updates them.

FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.30@sha256:538e0b39736e7feae937a65983e49d2ab75e1559d35041f9878b7b7e51de91e4 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ARG VERSION=0.0.0-dev
LABEL org.opencontainers.image.source="https://github.com/mcurcio/tool-approvals" \
      org.opencontainers.image.description="Human approval of MCP tool calls: the Call Gate and the Approvals service" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 65532:65532
ENTRYPOINT ["tool-approvals"]
