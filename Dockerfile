FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POLICY_DIR=/opt/fare/policies \
    AUDIT_LOG_DIR=/var/lib/fare/audit

WORKDIR /opt/fare
RUN addgroup --system fare && adduser --system --ingroup fare fare

COPY pyproject.toml README.md ./
COPY app ./app
COPY policies ./policies
COPY examples ./examples
RUN pip install --no-cache-dir .

RUN mkdir -p /var/lib/fare/audit && chown -R fare:fare /var/lib/fare
USER fare

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
