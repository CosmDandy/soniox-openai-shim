FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8756

WORKDIR /app

# requirements.txt is the full tree locked with hashes (pip-compile from
# requirements.in): an unpinned transitive dependency is resolved afresh on
# every build, which is how 0.3.0 shipped a certifi with revoked emSign roots.
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

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
