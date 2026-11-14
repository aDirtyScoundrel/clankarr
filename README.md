# clankarr

MCP server for the hades media stack. It cross-references Seerr, Radarr,
Sonarr and Jellyfin, and it controls the Seerr sync jobs.

## Tools

- clankarr_media_search: search Seerr by title.
- clankarr_media_status: one title across all four services, with a verdict.
- clankarr_request_list: list Seerr requests.
- clankarr_request_create: request a movie or show. Triggers a download.
- clankarr_jobs_list: Seerr job schedules and state.
- clankarr_job_run: run a Seerr job now.
- clankarr_job_set_schedule: change a job cron schedule (6 fields).
- clankarr_stack_drift: find stale "available" entries library-wide.

## How availability moves

Seerr has no inbound webhooks. The radarr-scan and sonarr-scan jobs promote
media to available. The Jellyfin recently-added scan promotes new Jellyfin
items every 5 minutes. Only the availability-sync job demotes deleted media.
Re-downloads of old library items need the arr scans, because the Jellyfin
fast path only sees items with a new creation date.

## Deployment

The server runs on hades as the Arcane project Clankarr. It joins the
jellyfin-server_homelab_net network and reaches the services by container
name. It reads API keys from /dockercompose/config at runtime. Keys never
leave hades and never appear in tool output.

The MCP endpoint is http://192.168.18.18:8099/mcp (streamable HTTP,
stateless). Every call except /health needs the bearer token from the
project .env file.

## Build

1. rsync this directory to hades.
2. docker build -t local/clankarr:1.0 .
3. docker compose --project-name clankarr -f /opt/arcane/projects/Clankarr/compose.yaml up -d

## Hermes wiring

hermes mcp add, or an mcp_servers.clankarr entry with url and an
Authorization header. A gateway restart loads the new tools.
