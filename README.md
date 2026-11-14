# clankarr

An MCP server for the *arr media stack. It cross-references Seerr, Radarr,
Sonarr and Jellyfin, and it controls the Seerr sync jobs. It answers "is
this title ready yet?" and "why does deleted media still show available?"
with one tool call instead of four API spelunking trips.

## Why this exists

Seerr has no inbound webhooks from Radarr or Sonarr. Availability moves
through scheduled jobs, and the stock schedules are daily. That makes new
downloads and deletions take up to 24 hours to reflect. clankarr exposes
those jobs as tools, plus a cross-referenced status view that shows where
the truth diverges between the four services.

## Tools

| Tool | What it does |
|------|--------------|
| `media_search` | Search Seerr for movies and shows by title. |
| `media_status` | One title across Seerr, the arr, and Jellyfin, with a drift verdict. |
| `request_list` | List Seerr requests, newest first. |
| `request_create` | Request a movie or show. Refuses duplicates and available titles. |
| `jobs_list` | Seerr job schedules, next runs, and state. |
| `job_run` | Run a Seerr job now. |
| `job_set_schedule` | Change a job's cron schedule (6 fields, seconds first). |
| `stack_drift` | Library-wide check for stale "available" entries. |

Read tools are marked `readOnlyHint`. `request_create` triggers a download;
the job tools change Seerr state. There are no delete tools.

## Requirements

- Seerr, Radarr, Sonarr and Jellyfin instances reachable over HTTP.
- Their config files, readable by the server. API keys are read from them
  at runtime and never returned by any tool:
  - `<config>/seerr/settings.json` (`main.apiKey`, `jellyfin.apiKey`)
  - `<config>/radarr/config.xml` (`ApiKey`)
  - `<config>/sonarr/config.xml` (`ApiKey`)
- Docker, or Python 3.12+.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `CLANKARR_TOKEN` | none, required | Bearer token for the MCP endpoint. The server refuses to start without it. |
| `CLANKARR_PORT` | `8099` | Listen port. |
| `CLANKARR_CONFIG_DIR` | `/config` | Directory holding the service configs listed above. |
| `SEERR_URL` | `http://seerr:5055` | Seerr base URL. |
| `RADARR_URL` | `http://radarr:7878` | Radarr base URL. |
| `SONARR_URL` | `http://sonarr:8989` | Sonarr base URL. |
| `JELLYFIN_URL` | `http://jellyfin:8096` | Jellyfin base URL. |

The default URLs assume container names on a shared Docker network. Point
them at host addresses if your services live elsewhere.

## Run with Docker

```bash
docker build -t clankarr .
docker run -d --name clankarr \
  -p 8099:8099 \
  -e CLANKARR_TOKEN="$(openssl rand -hex 32)" \
  -v /path/to/service/configs:/config:ro \
  clankarr
```

Or use the provided `compose.yaml`. Set `CLANKARR_TOKEN` in a `.env` file
next to it, and override `CLANKARR_CONFIG_DIR`, `CLANKARR_NETWORK`,
`CLANKARR_HOST_PORT` and `TZ` as needed. `CLANKARR_NETWORK` must be an
existing external network that can reach your services, or set the four
service URLs to reachable addresses instead.

## Run from source

```bash
pip install -r requirements.txt
CLANKARR_TOKEN="$(openssl rand -hex 32)" python clankarr.py
```

## Connect an MCP client

The endpoint is streamable HTTP at `http://<host>:8099/mcp`. Every request
except `/health` needs the bearer token. Example client config:

```yaml
mcp_servers:
  clankarr:
    url: http://<host>:8099/mcp
    headers:
      Authorization: Bearer <your token>
```

`GET /health` is unauthenticated and reports version plus config presence,
for container healthchecks.

## How Seerr availability works

Operational knowledge worth having before you change schedules:

- Promotion to available: `radarr-scan` and `sonarr-scan` poll the arrs and
  promote any known media with a file. The Jellyfin recently-added scan
  promotes items too, but only items with a NEW Jellyfin creation date.
- Re-downloads of titles already in the library keep their old creation
  date, so only the arr scans catch them.
- Demotion: `availability-sync` is the only job that marks deleted media.
  It requires the title to be absent from BOTH Jellyfin and the arr. The
  arr scans never downgrade an available title.
- Empty folders left on disk keep a Jellyfin Folder item alive, which
  blocks demotion. Delete empty folders if deleted titles stay available.
- A full availability sync is cheap (seconds for a large library). Running
  all three jobs every 5 minutes is a sane default:
  `job_set_schedule(job_id="availability-sync", schedule="0 */5 * * * *")`.
- Jellyfin's root `/Items` endpoint under-reports the library (it drops
  box-set members). clankarr queries user-scoped and paginated, so drift
  results are not polluted by that trap.

## Security notes

- Keep the server on a trusted network. The token gates the MCP endpoint,
  but the service API keys behind it are admin-grade.
- Keys are read from the mounted configs at runtime, cached by file mtime,
  and never appear in tool output or logs.
- Mount the config directory read-only.

## Credits

Designed and built by Hadaly, a Hermes agent, on 2026-11-13 in a single
diagnosis-and-build session.

## License

MIT. See LICENSE.
