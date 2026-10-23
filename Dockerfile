FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates nodejs && rm -rf /var/lib/apt/lists/*
WORKDIR /srv/app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY web/ ./web/
RUN useradd --system --uid 10001 --create-home yt && mkdir -p /data && chown -R yt:yt /data /srv/app
USER yt
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=3)' || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
