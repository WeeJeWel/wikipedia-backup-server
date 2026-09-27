# wikipedia-backup-server

One Docker container downloads the latest full Wikipedia `all_nopic` ZIM for
the selected language and serves it as HTML through Kiwix. The archive stays
compressed; it is not extracted to millions of files. There are no diffs or
images. On an empty `/data` volume, the HTTP server starts immediately with a
progress page that refreshes as the file downloads and is verified.

## Run on the Raspberry Pi

Clone the repository and run Compose from that directory:

```bash
mkdir -p /mnt/nvme1n1p1/Config/Kiwix
docker compose pull
docker compose up -d
docker compose logs -f wikipedia-backup-server
```

Visit `http://<pi-ip>:8080/`. The first download starts automatically. If a
matching `.zim` is already in the mounted directory, it is verified and served;
no manual initial download is required. `/progress` always displays status,
and `/status` returns JSON. The first full download is about 49 GB for English.
Allow space for both the current and replacement ZIM during an update.

| Variable | Default | Meaning |
| --- | --- | --- |
| `LANGUAGE` | `en` | Wikipedia language code (for example `de`, `fr`) |
| `SCHEDULE` | `0 3 1 * *` | Five-field cron expression; 03:00 on the first of each month |
| `PORT` | `8080` | HTTP port inside the container and on the host in Compose |
| `TZ` | `Europe/Amsterdam` | Time zone used by the schedule |

Create an optional `.env` next to `compose.yaml` to override values:

```dotenv
LANGUAGE=en
SCHEDULE=0 3 1 * *
PORT=8080
TZ=Europe/Amsterdam
```

The container checks for a newer release at startup and on schedule. A failed
check/download is retried after 10 minutes without an archive, or after one
hour when an older archive is available. A partial download is kept as
`.zim.part` and resumed when supported
by the server. The downloaded file must pass the ZIM internal checksum check
before it is served. During an update the old version stays available. The
old file is deleted only after the replacement web server responds. If the
replacement fails to start, the old version is restored.

Do not expose this unauthenticated server to the public Internet without an
appropriate access layer. The archive is periodically replaced; if you need
independent backups or retention, back up `/data` separately.

## Build notes

The image builds on the Pi with Debian packages for `kiwix-serve` and
`zimcheck` (arm64 and armhf available). No Docker socket, cron daemon, or
second container is needed. The Python process owns the schedule, progress
page, download, validation, and Kiwix process. A small HTTP proxy lets the
progress page and Kiwix use the same public port.

If you prefer to build locally, run
`docker build -t wikipedia-backup-server:local .` and change the image in
`compose.yaml` to `wikipedia-backup-server:local`.

Pushing to `main` runs the tests and publishes `latest` and a commit SHA tag
to `ghcr.io/weejewel/wikipedia-backup-server` for amd64, arm64, and arm/v7.
