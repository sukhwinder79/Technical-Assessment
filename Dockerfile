# =============================================================================
#  Dockerised deployment of the agent's HTTP + MCP surface.
#
#  IMPORTANT: Microsoft Excel is a Windows desktop application and cannot run in
#  a Linux container. Inside this image `excel_import_csv` automatically falls
#  back to the openpyxl writer and reports `excel_launched: false`, which the
#  agent surfaces honestly in its final report. Real Excel COM automation
#  requires running the agent natively on Windows (see README → Running on
#  Windows). The container is the right home for the API/Google Sheets half of
#  the workflow and for CI.
# =============================================================================

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so the layer caches across source edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY config/ ./config/
RUN pip install --no-cache-dir --no-deps -e .

# Non-root, with a writable workspace.
RUN useradd --create-home --shell /bin/bash agent \
 && mkdir -p /app/workspace /app/logs /app/.agent_memory /app/credentials \
 && chown -R agent:agent /app
USER agent

ENV WORKSPACE_DIR=/app/workspace \
    LOG_DIR=/app/logs \
    MEMORY_DIR=/app/.agent_memory \
    TOOLS_CONFIG=/app/config/tools.yaml \
    LOG_JSON=true \
    EXCEL_VISIBLE=false \
    EXCEL_KEEP_OPEN=false

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "agentic_sheets.api.server:app", "--host", "0.0.0.0", "--port", "8000"]
