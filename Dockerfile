FROM python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8756

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY soniox_openai_shim.py ./

RUN useradd --create-home --uid 10001 shim \
    && mkdir -p /data && chown shim:shim /data
USER shim

VOLUME ["/data"]

EXPOSE 8756

# python-only probe: the slim image ships no curl or wget.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8756/health', timeout=3).status == 200 else 1)"]

CMD ["uvicorn", "soniox_openai_shim:app", "--host", "0.0.0.0", "--port", "8756"]
