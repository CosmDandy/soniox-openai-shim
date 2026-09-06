FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8756

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY soniox_shim.py ./

RUN useradd --create-home --uid 10001 shim \
    && mkdir -p /data && chown shim:shim /data
USER shim

VOLUME ["/data"]

EXPOSE 8756

# python-only probe: the slim image ships no curl or wget.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8756/health', timeout=3).status == 200 else 1)"]

CMD ["uvicorn", "soniox_shim:app", "--host", "0.0.0.0", "--port", "8756"]
