# Telegram → Stremio

A FastAPI addon and HTTP byte-range server backed by a Telethon **user session**.
It indexes movie videos in selected Telegram channels/groups and proxies the
original MKV/MP4 file directly to Stremio. No transcoding or full-file download.

```text
Stremio ── manifest / catalog / meta / stream ──▶ FastAPI + SQLite index
Player  ── GET video, Range: bytes=x-y ─────────▶ Telethon ──▶ Telegram
        ◀─ 206 + only the requested bytes ──────┘
```

## Local Docker setup

1. Get an API ID and API hash from <https://my.telegram.org/apps>. Your Telegram
   account must already be a member of the source channels/groups.
2. Copy `.env.example` to `.env` and fill in `TELEGRAM_API_ID` and
   `TELEGRAM_API_HASH`.

   PowerShell: `Copy-Item .env.example .env`

   Linux/macOS: `cp .env.example .env`

3. Build the image and complete the interactive Telegram login:

   ```sh
   docker compose build
   docker compose run --rm addon python -m app.cli login
   docker compose run --rm addon python -m app.cli chats
   ```

   Enter your phone number, Telegram login code, and 2FA password if enabled.
   The session and database persist in the `telegram-data` Docker volume.

4. Set `TELEGRAM_CHATS` in `.env` using the signed channel/group IDs printed by
   `chats` (e.g. `-1001234567890,-1009876543210`), or public `@usernames`.
5. Start the service:

   ```sh
   docker compose up -d
   docker compose logs -f addon
   ```

6. Install `http://localhost:8000/manifest.json` in **Stremio Desktop** via its
   addon URL field. If `ADDON_TOKEN` is configured, the URL is
   `http://localhost:8000/<token>/manifest.json`. To print it:

   ```sh
   docker compose exec addon python -m app.cli url
   ```

Initial indexing runs in the background; the catalog fills as videos are found.
Addon JSON responses disable HTTP caching so an empty catalog fetched during
startup is not reused after videos have been indexed.
Open `/health` (or `/<token>/health`) to see the file count, last completed sync,
and indexing error status. A running HTTP service can have an indexing error;
inspect `sync_error` and the logs rather than relying only on Docker health.

### Indexed videos missing in Stremio Desktop

Open `http://127.0.0.1:8000/catalog/movie/telegram.json` in your browser (include
the token prefix if configured). If the video appears in `metas`, it is already
indexed; a full Telegram re-scan is unnecessary.

Remove the existing Telegram Movies addon in Stremio, fully close and reopen
Stremio, then reinstall `http://127.0.0.1:8000/manifest.json`. In **Discover**, select
**Movies → Telegram Movies**. This also avoids an IPv6 `localhost` connection on
systems where Docker is published only on IPv4. The search endpoint
`/catalog/movie/telegram/search=Varavu.json` can verify a particular indexed title.

If the catalog loads but a movie reports **No streams found**, check its
`/stream/movie/<catalog-id>.json` response. Stream objects must contain only one
of `title` or `description`: Stremio treats them as aliases and rejects responses
that contain both. Version 1.0.2 fixes this. Update the service and reopen the
movie; fully restart Stremio if it retains the earlier stream error.

### Stremio on another device

`localhost` refers to the **player's** device. For a TV/phone on your LAN, set
`PUBLIC_BASE_URL=http://<computer-LAN-IP>:8000` and change Compose's port binding
to `"8000:8000"`, then recreate the service. Local plain HTTP is intended for
desktop/native testing; Stremio Web expects HTTPS, and browser playback depends
on codec support. The addon marks original files `notWebReady` conservatively.

## Movie discovery and IMDb matching

- The initial scan covers the latest `HISTORY_LIMIT` **messages** per source
  (default 1000). Set `0` to scan all accessible history.
- New messages and edits are indexed live. Periodic catch-up scans use persisted
  per-chat checkpoints; checkpoints advance only after indexing succeeds.
- Periodic reconciliation refreshes the latest 100 indexed messages per source.
  Deletions with known chat IDs are handled live. Older edits/deletions missed
  while offline are discovered when played or by a full reindex; a deleted file
  returns 404, and changed media returns 409 until its index entry refreshes.
- Movie filenames are parsed with GuessIt. Detected TV episode files are skipped;
  this first version implements movies. Archives and external video links are
  not playable files in this index.
- A single `tt...` IMDb ID in the filename/caption wins over automatic matching.
  **Manual mappings have highest priority** and survive reindexing.
- Otherwise, Cinemeta is searched using the parsed title. Only a unique exact
  normalized title match, with the same year when supplied, is accepted. Search
  results and metadata are cached for 24 hours. Failed/ambiguous lookup leaves
  the file playable in the Telegram catalog without an IMDb association.
- Cinemeta supplies posters and descriptive metadata when available. Set
  `METADATA_LOOKUP=false` to use Telegram filename/caption metadata only; explicit
  IMDb IDs and manual mappings still supply streams to regular movie pages.
- The Telegram catalog has stable IDs like `tg:-1001234567890:456`. It supports
  search and pages of 100 items. Files with the same IMDb mapping appear as
  alternative streams, ordered by labeled resolution and then file size.

### Full scan and manual mappings

Stop the server before opening its Telegram session from a separate CLI process.
Use **one worker/replica** for this session and index.

```sh
docker compose stop addon
docker compose run --rm addon python -m app.cli sync --full
docker compose run --rm addon python -m app.cli unmatched
docker compose run --rm addon python -m app.cli map --chat-id=-1001234567890 --message-id=456 --imdb-id=tt0133093
docker compose up -d
```

A full scan refreshes all accessible messages. For very old deleted messages
whose IDs are not present in history, playback still validates against Telegram
before serving bytes. The first implementation does not sweep every historical
indexed ID for deletions.

## HTTP endpoints

All addon/video endpoints use `/<ADDON_TOKEN>` as a prefix when configured:

```text
GET       /manifest.json
GET       /catalog/movie/telegram.json
GET       /catalog/movie/telegram/search=The%20Matrix&skip=0.json
GET       /meta/movie/tg:-1001234567890:456.json
GET       /stream/movie/tg:-1001234567890:456.json
GET       /stream/movie/tt0133093.json
GET, HEAD /video/-1001234567890/456
GET       /health
```

Video requests require a configured chat and an indexed movie message. The server
refetches the message to obtain current file references and validate document ID
and size. A stream response contains an absolute URL based on `PUBLIC_BASE_URL`.

```json
{
  "streams": [
    {
      "name": "Telegram",
      "title": "The.Matrix.1999.1080p.mkv\n1080p • H.264 • 2.00 GiB",
      "url": "http://localhost:8000/video/-1001234567890/456",
      "behaviorHints": {
        "notWebReady": true,
        "filename": "The.Matrix.1999.1080p.mkv",
        "videoSize": 2147483648
      }
    }
  ]
}
```

### Range behavior

- `GET` without `Range`: `200` with the file's full `Content-Length`.
- Closed, open-ended, and suffix byte ranges: `206`, `Content-Range`, and exact
  `Content-Length`. Ends beyond EOF are clamped.
- Unsatisfiable/malformed single byte ranges: `416`, `Content-Range: bytes */size`.
- Multipart requests and unknown units: ignored, returning the full `200`
  representation. Multipart responses are not needed for Stremio seeking.
- `HEAD`: the same full-file headers as `GET`, no Telegram file-chunk reads.
  HTTP requires ranges to be ignored for methods other than GET.
- `If-Range`: matched against the document's strong ETag or Last-Modified;
  a mismatch yields the full file instead of a partial representation.
- Telegram requests start at the 512 KiB-aligned chunk containing the desired
  offset. Only the prefix within that chunk is discarded: seeking does **not**
  download everything from byte zero. The output is clipped to the exact range.
- Expired file references are refreshed and resumed at the current offset.
  Telegram CDN redirects are decrypted with AES-CTR and checked against origin
  SHA-256 hashes before bytes are delivered. This adapter uses a small pinned
  Telethon private API surface; rerun reader tests before upgrading Telethon.
- Readers close and streaming slots release on completion, disconnect, or error.
  At most `MAX_STREAMS` transfers run at once; saturation returns `503`.
- Failures before the first chunk return `429`/`502`. Failures after headers are
  sent terminate the connection so a player can retry its range request.

PowerShell example (use `curl.exe`, not the PowerShell `curl` alias):

```sh
curl.exe -I http://localhost:8000/video/-1001234567890/456
curl.exe -D - -H "Range: bytes=734003200-734004223" http://localhost:8000/video/-1001234567890/456 -o seek.bin
```

Expect 206, a 1024-byte body, and
`Content-Range: bytes 734003200-734004223/<actual-file-size>`.

## Run without Docker

Requires Python 3.12+. On Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m app.cli login
.\.venv\Scripts\python.exe -m app.cli chats
.\.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

The same CLI commands work with `python -m app.cli ...` on other platforms.
Local runs use `data/telegram.session` and `data/index.sqlite3`. Docker uses its
own named volume; a local session is not automatically imported into Docker.

## Move to a VPS

Point your domain at the VPS, terminate HTTPS in your reverse proxy, and set
`PUBLIC_BASE_URL=https://stream.yourdomain.com`. Proxy to `127.0.0.1:8000` and
preserve `Range`, `If-Range`, and response headers. For Nginx disable response
buffering (`proxy_buffering off`) and allow a long streaming read timeout
(`proxy_read_timeout 3600s`). The app also sends `X-Accel-Buffering: no`.

Set a random `ADDON_TOKEN` to restrict access via the shared secret URL; treat
the install URL as a credential. Access logging is disabled by the supplied
Uvicorn command because URLs can include the token. Keep the Telegram session
and `.env` private. This is a single-user service, not a multi-tenant login system.

Recreate the service after environment changes and reinstall the addon if its
domain or token changes. Keep the persistent Docker volume when updating.

## Development checks

```sh
python -m pip install -r requirements-dev.txt
python -m pytest
python -m ruff check app tests
```

Tests use simulated Telegram documents and HTTP requests; they exercise seeking,
range boundaries, cleanup, reference renewal, authenticated CDN reads, indexing
checkpoints, IMDb matching, and addon responses without a Telegram account.
Actual Telegram playback and Stremio-device codec compatibility require your
configured session and a real video.
