FROM python:3.13-slim-trixie

LABEL org.opencontainers.image.source="https://github.com/WeeJeWel/wikipedia-backup-server" \
      org.opencontainers.image.description="Offline Wikipedia no-images download and Kiwix server"

RUN apt-get update \
    && apt-get install -y --no-install-recommends kiwix-tools zim-tools aria2 ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server.py /app/server.py

ENV LANGUAGE=en \
    SCHEDULE="0 3 1 * *" \
    PORT=8080 \
    DOWNLOAD_METHOD=torrent \
    TZ=Europe/Amsterdam \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8080
STOPSIGNAL SIGTERM
CMD ["python", "/app/server.py"]
