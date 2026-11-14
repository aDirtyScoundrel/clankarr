#!/usr/bin/env python3
"""clankarr - MCP server for the hades media stack.

Cross-references Seerr, Radarr, Sonarr and Jellyfin. API keys are read
from the mounted service configs at runtime and never leave this host.
"""
import base64
import json
import os
import xml.etree.ElementTree as ET
from typing import Annotated, Any, Optional

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from pydantic import Field
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

VERSION = "1.3.0"
PORT = int(os.environ.get("CLANKARR_PORT", "8099"))
TOKEN = os.environ.get("CLANKARR_TOKEN", "")
CONFIG_DIR = os.environ.get("CLANKARR_CONFIG_DIR", "/config")
SEERR_URL = os.environ.get("SEERR_URL", "http://seerr:5055")
RADARR_URL = os.environ.get("RADARR_URL", "http://radarr:7878")
SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr:8989")
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://jellyfin:8096")

MEDIA_STATUS = {1: "unknown", 2: "pending", 3: "processing",
                4: "partially available", 5: "available",
                6: "blocklisted", 7: "deleted"}
REQ_STATUS = {1: "pending approval", 2: "approved", 3: "declined",
              4: "failed", 5: "completed"}

http = httpx.AsyncClient(timeout=90.0)

mcp = FastMCP(
    "clankarr",
    instructions=(
        "Media stack tools for Seerr, Radarr, Sonarr and Jellyfin. "
        "Use media_status for one title, stack_drift "
        "for library-wide staleness. Availability is promoted by the "
        "radarr-scan/sonarr-scan jobs and demoted only by the "
        "availability-sync job. Seerr has no inbound webhooks."),
    host="0.0.0.0",
    port=PORT,
    stateless_http=True,
)

# ---------------------------------------------------------------- keys


class Keys:
    """Runtime API keys, cached by config mtime. Never returned by tools."""

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}
        self._mtimes: dict[str, float] = {}

    def _fresh(self, path: str) -> bool:
        try:
            m = os.path.getmtime(path)
        except OSError:
            return False
        if self._mtimes.get(path) != m:
            self._mtimes[path] = m
            return True
        return False

    def load(self) -> None:
        sp = f"{CONFIG_DIR}/seerr/settings.json"
        if self._fresh(sp) or "seerr" not in self._cache:
            with open(sp) as f:
                s = json.load(f)
            self._cache["seerr"] = s["main"]["apiKey"]
            self._cache["jellyfin"] = s["jellyfin"]["apiKey"]
        for name in ("radarr", "sonarr"):
            p = f"{CONFIG_DIR}/{name}/config.xml"
            if self._fresh(p) or name not in self._cache:
                self._cache[name] = ET.parse(p).getroot().find("ApiKey").text.strip()

    def get(self, service: str) -> str:
        self.load()
        return self._cache[service]


keys = Keys()

# ---------------------------------------------------------------- http helpers


class ApiError(Exception):
    def __init__(self, service: str, status: int, path: str) -> None:
        self.service, self.status, self.path = service, status, path
        super().__init__(f"{service} HTTP {status} on {path}")


async def api(service: str, method: str, path: str,
              body: Optional[dict] = None, params: Optional[dict] = None,
              _retry: bool = True) -> Any:
    base = {"seerr": SEERR_URL, "radarr": RADARR_URL,
            "sonarr": SONARR_URL, "jellyfin": JELLYFIN_URL}[service]
    key = keys.get(service)
    if service == "jellyfin":
        headers = {"Authorization": f'MediaBrowser Token="{key}"'}
    else:
        headers = {"X-Api-Key": key}
    r = await http.request(method, base + path, headers=headers,
                           json=body, params=params)
    if r.status_code == 401 and _retry:
        keys._mtimes.clear()  # force key reload, retry once
        return await api(service, method, path, body, params, _retry=False)
    if r.status_code >= 400:
        raise ApiError(service, r.status_code, path)
    if r.status_code == 204 or not r.content:
        return None
    return r.json()


def err(e: Exception) -> str:
    if isinstance(e, ApiError):
        hint = {401: "API key rejected; check service config.",
                404: "Not found; check the id or job name.",
                400: "Bad request; check parameter values."}.get(
                    e.status, "Check service logs.")
        return json.dumps({"error": str(e), "hint": hint})
    if isinstance(e, httpx.TimeoutException):
        return json.dumps({"error": "request timed out",
                           "hint": "Retry, or check the service is up."})
    return json.dumps({"error": f"{type(e).__name__}: {e}"})


def out(data: Any) -> str:
    return json.dumps(data, indent=1, default=str)


# ---------------------------------------------------------------- jellyfin helpers

_jf_uid: Optional[str] = None


async def jf_uid() -> str:
    """Resolve the Jellyfin admin user id (cached).

    The root /Items endpoint under-reports the library (it drops some
    box-set members). The user-scoped /Users/{id}/Items returns the full
    set, so every Jellyfin query must run in a user context.
    """
    global _jf_uid
    if _jf_uid is None:
        users = await api("jellyfin", "GET", "/Users")
        admin = next((u for u in users
                      if (u.get("Policy") or {}).get("IsAdministrator")),
                     users[0])
        _jf_uid = admin["Id"]
    return _jf_uid


async def jf_all_items(item_type: str) -> list[dict]:
    """Fetch every Jellyfin item of a type, paginated, in user context.

    A single unpaginated /Items call can return a partial set. Always
    page until TotalRecordCount is reached.
    """
    uid = await jf_uid()
    items: list[dict] = []
    start = 0
    while True:
        d = await api("jellyfin", "GET", f"/Users/{uid}/Items", params={
            "Recursive": "true", "IncludeItemTypes": item_type,
            "Fields": "ProviderIds,DateCreated",
            "StartIndex": start, "Limit": 500})
        batch = d.get("Items", [])
        items.extend(batch)
        total = d.get("TotalRecordCount", len(items))
        start += len(batch)
        if not batch or start >= total:
            break
    return items


def jf_ids(items: list[dict]) -> dict[str, set[int]]:
    ids: dict[str, set[int]] = {"Tmdb": set(), "Tvdb": set(), "Imdb": set()}
    for it in items:
        p = it.get("ProviderIds") or {}
        for k in ids:
            v = p.get(k)
            if v:
                try:
                    ids[k].add(int(v))
                except ValueError:
                    pass
    return ids


async def jf_find(title: str, tmdb: Optional[int] = None,
                  tvdb: Optional[int] = None) -> list[dict]:
    uid = await jf_uid()
    d = await api("jellyfin", "GET", f"/Users/{uid}/Items", params={
        "Recursive": "true", "SearchTerm": title,
        "Fields": "ProviderIds,DateCreated"})
    hits = []
    for it in d.get("Items", []):
        if it.get("Type") not in ("Movie", "Series", "Folder"):
            continue
        p = it.get("ProviderIds") or {}
        match = False
        if tmdb is not None and str(tmdb) == str(p.get("Tmdb", "")):
            match = True
        if tvdb is not None and str(tvdb) == str(p.get("Tvdb", "")):
            match = True
        if match:
            hits.append({"name": it.get("Name"), "type": it.get("Type"),
                         "created": (it.get("DateCreated") or "")[:19]})
    return hits


# ---------------------------------------------------------------- tools


@mcp.tool(name="media_search", annotations={
    "title": "Search media", "readOnlyHint": True, "destructiveHint": False,
    "idempotentHint": True, "openWorldHint": True})
async def media_search(
    query: Annotated[str, Field(min_length=1, max_length=200,
                                description="Title to search for, e.g. 'Dune'")],
    limit: Annotated[int, Field(ge=1, le=25,
                                description="Max results")] = 8,
) -> str:
    """Search Seerr for movies and TV shows by title.

    Returns matches with tmdbId, year and current Seerr status. Use the
    tmdbId with media_status or request_create. Person
    results are filtered out.
    """
    try:
        d = await api("seerr", "GET", "/api/v1/search",
                      params={"query": query, "page": 1})
        results = []
        for r in d.get("results", []):
            mt = r.get("mediaType")
            if mt not in ("movie", "tv"):
                continue
            mi = r.get("mediaInfo") or {}
            results.append({
                "mediaType": mt, "tmdbId": r.get("id"),
                "title": r.get("title") or r.get("name"),
                "year": (r.get("releaseDate") or r.get("firstAirDate") or "")[:4],
                "status": MEDIA_STATUS.get(mi.get("status"), "not requested")})
            if len(results) >= limit:
                break
        return out({"query": query, "count": len(results),
                    "results": results})
    except Exception as e:
        return err(e)


@mcp.tool(name="media_status", annotations={
    "title": "Cross-referenced media status", "readOnlyHint": True,
    "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def media_status(
    media_type: Annotated[str, Field(pattern="^(movie|tv)$")],
    tmdb_id: Annotated[int, Field(ge=1, description="TMDB id, not the Seerr media id")],
) -> str:
    """Full status of one title across Seerr, Radarr/Sonarr and Jellyfin.

    Returns the Seerr status and requests, the arr file state, Jellyfin
    presence, and a verdict. A title can be 'available' in Seerr while
    deleted on disk: the verdict flags that drift.
    """
    try:
        mt, tmdb = media_type, tmdb_id
        d = await api("seerr", "GET", f"/api/v1/{mt}/{tmdb}")
        mi = d.get("mediaInfo") or {}
        title = d.get("title") or d.get("name") or f"tmdb:{tmdb}"
        seerr = {
            "status": MEDIA_STATUS.get(mi.get("status"), "not requested"),
            "statusCode": mi.get("status"),
            "updatedAt": mi.get("updatedAt"),
            "requests": [{"status": REQ_STATUS.get(q.get("status")),
                          "id": q.get("id")} for q in mi.get("requests", [])]}
        arr: dict[str, Any] = {}
        tvdb = mi.get("tvdbId")
        if mt == "movie":
            movies = await api("radarr", "GET", "/api/v3/movie")
            m = next((x for x in movies if x.get("tmdbId") == tmdb), None)
            if m is None:
                arr = {"inRadarr": False}
            else:
                mf = m.get("movieFile") or {}
                arr = {"inRadarr": True, "hasFile": m.get("hasFile"),
                       "monitored": m.get("monitored"),
                       "fileAdded": mf.get("dateAdded"),
                       "radarrId": m.get("id")}
        else:
            series = await api("sonarr", "GET", "/api/v3/series")
            s = next((x for x in series if x.get("tvdbId") == tvdb), None)
            if s is None:
                arr = {"inSonarr": False, "tvdbId": tvdb}
            else:
                st = s.get("statistics") or {}
                arr = {"inSonarr": True, "tvdbId": tvdb,
                       "episodeFiles": st.get("episodeFileCount"),
                       "episodes": st.get("episodeCount"),
                       "monitored": s.get("monitored")}
        jf = await jf_find(title, tmdb=tmdb if mt == "movie" else None,
                           tvdb=tvdb)
        in_arr = arr.get("inRadarr", arr.get("inSonarr", False))
        has_file = arr.get("hasFile") if mt == "movie" else (
            (arr.get("episodeFiles") or 0) > 0)
        problems = []
        if seerr["statusCode"] == 5:
            if not in_arr:
                problems.append("available in Seerr but absent from the arr")
            elif not has_file:
                problems.append("available in Seerr but no file in the arr")
            if not jf:
                problems.append("not in Jellyfin (or missing provider ids)")
        verdict = ("DRIFT: " + "; ".join(problems) +
                   ". Run job_run('availability-sync') to reconcile."
                   if problems else "consistent")
        return out({"title": title, "mediaType": mt, "tmdbId": tmdb,
                    "seerr": seerr, "arr": arr, "jellyfin": jf,
                    "verdict": verdict})
    except Exception as e:
        return err(e)


@mcp.tool(name="request_list", annotations={
    "title": "List requests", "readOnlyHint": True, "destructiveHint": False,
    "idempotentHint": True, "openWorldHint": True})
async def request_list(
    take: Annotated[int, Field(ge=1, le=100)] = 20,
    filter: Annotated[str, Field(
        description="all | pending | approved | processing | available")] = "all",
) -> str:
    """List Seerr requests, newest first."""
    try:
        d = await api("seerr", "GET", "/api/v1/request",
                      params={"take": take, "sort": "added",
                              "filter": filter})
        rows = []
        for q in d.get("results", []):
            m = q.get("media") or {}
            rows.append({"requestId": q.get("id"), "type": q.get("type"),
                         "tmdbId": m.get("tmdbId"),
                         "status": REQ_STATUS.get(q.get("status")),
                         "mediaStatus": MEDIA_STATUS.get(m.get("status"))})
        return out({"count": len(rows), "requests": rows})
    except Exception as e:
        return err(e)


@mcp.tool(name="request_create", annotations={
    "title": "Create a media request", "readOnlyHint": False,
    "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
async def request_create(
    media_type: Annotated[str, Field(pattern="^(movie|tv)$")],
    tmdb_id: Annotated[int, Field(ge=1)],
    seasons: Annotated[str, Field(
        description="TV only: 'all' or comma list like '1,2'")] = "all",
    profile_id: Annotated[int | None, Field(
        description="Optional arr quality profile id for this request")] = None,
) -> str:
    """Request a movie or TV show. This triggers a download in Radarr/Sonarr.

    Refuses titles that are already available or already have an open
    request.
    """
    try:
        mt, tmdb = media_type, tmdb_id
        d = await api("seerr", "GET", f"/api/v1/{mt}/{tmdb}")
        mi = d.get("mediaInfo") or {}
        if mi.get("status") == 5:
            return out({"ok": False,
                        "reason": "already available in Seerr; not re-requested"})
        for q in mi.get("requests", []):
            if q.get("status") in (1, 2):
                return out({"ok": False,
                            "reason": "open request already exists",
                            "requestId": q.get("id"),
                            "status": REQ_STATUS.get(q.get("status"))})
        body: dict[str, Any] = {"mediaType": mt, "mediaId": tmdb, "is4k": False}
        if mt == "tv":
            body["seasons"] = (seasons if seasons == "all"
                               else [int(s) for s in seasons.split(",")])
        if profile_id is not None:
            body["profileId"] = profile_id
        r = await api("seerr", "POST", "/api/v1/request", body=body)
        return out({"ok": True, "requestId": r.get("id"),
                    "status": REQ_STATUS.get(r.get("status")),
                    "type": r.get("type")})
    except Exception as e:
        return err(e)


async def _request_action(request_id: int, action: str) -> str:
    try:
        r = await api("seerr", "POST",
                      f"/api/v1/request/{request_id}/{action}", body={})
        return out({"ok": True, "requestId": request_id,
                    "status": REQ_STATUS.get((r or {}).get("status"))})
    except Exception as e:
        return err(e)


@mcp.tool(name="request_approve", annotations={
    "title": "Approve a request", "readOnlyHint": False,
    "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def request_approve(
    request_id: Annotated[int, Field(ge=1, description="Seerr request id")],
) -> str:
    """Approve a pending Seerr request. The arr then searches for it.

    Goes through Seerr; never bypasses the request system.
    """
    return await _request_action(request_id, "approve")


@mcp.tool(name="request_decline", annotations={
    "title": "Decline a request", "readOnlyHint": False,
    "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def request_decline(
    request_id: Annotated[int, Field(ge=1, description="Seerr request id")],
) -> str:
    """Decline a pending Seerr request. No media or files are touched."""
    return await _request_action(request_id, "decline")


@mcp.tool(name="request_retry", annotations={
    "title": "Retry a request", "readOnlyHint": False,
    "destructiveHint": False, "idempotentHint": False, "openWorldHint": True})
async def request_retry(
    request_id: Annotated[int, Field(ge=1, description="Seerr request id")],
) -> str:
    """Retry a failed Seerr request: re-triggers the arr search via Seerr."""
    return await _request_action(request_id, "retry")


@mcp.tool(name="request_delete", annotations={
    "title": "Delete a request record", "readOnlyHint": False,
    "destructiveHint": True, "idempotentHint": True, "openWorldHint": True})
async def request_delete(
    request_id: Annotated[int, Field(ge=1, description="Seerr request id")],
) -> str:
    """Delete a Seerr request RECORD only. Never deletes media or files.

    Use to clear declined/failed requests from the list. The library is
    untouched.
    """
    try:
        await api("seerr", "DELETE", f"/api/v1/request/{request_id}")
        return out({"ok": True, "requestId": request_id,
                    "note": "request record removed; media/files untouched"})
    except Exception as e:
        return err(e)


@mcp.tool(name="jobs_list", annotations={
    "title": "List Seerr jobs", "readOnlyHint": True, "destructiveHint": False,
    "idempotentHint": True, "openWorldHint": True})
async def jobs_list() -> str:
    """List Seerr scheduled jobs with cron schedule, next run and state.

    Key jobs: radarr-scan and sonarr-scan promote media to available;
    availability-sync demotes deleted media; jellyfin-recently-added-scan
    promotes newly added Jellyfin items.
    """
    try:
        jobs = await api("seerr", "GET", "/api/v1/settings/jobs")
        return out({"jobs": [
            {"id": j.get("id"), "name": j.get("name"),
             "schedule": j.get("cronSchedule"),
             "nextRun": j.get("nextExecutionTime"),
             "running": j.get("running")} for j in jobs]})
    except Exception as e:
        return err(e)


@mcp.tool(name="job_run", annotations={
    "title": "Run a Seerr job now", "readOnlyHint": False,
    "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def job_run(
    job_id: Annotated[str, Field(
        description="e.g. radarr-scan, sonarr-scan, availability-sync")],
) -> str:
    """Trigger a Seerr job immediately (returns at once; job runs async).

    Use availability-sync to clear stale 'available' entries after
    deletions, radarr-scan/sonarr-scan to promote fresh imports.
    """
    try:
        jobs = await api("seerr", "GET", "/api/v1/settings/jobs")
        if not any(j.get("id") == job_id for j in jobs):
            return out({"error": f"unknown job '{job_id}'",
                        "valid": [j.get("id") for j in jobs]})
        r = await api("seerr", "POST",
                      f"/api/v1/settings/jobs/{job_id}/run", body={})
        return out({"ok": True, "id": r.get("id"),
                    "running": r.get("running"),
                    "nextRun": r.get("nextExecutionTime")})
    except Exception as e:
        return err(e)


@mcp.tool(name="job_set_schedule", annotations={
    "title": "Set a Seerr job schedule", "readOnlyHint": False,
    "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def job_set_schedule(
    job_id: Annotated[str, Field(...)],
    schedule: Annotated[str, Field(
        description="6-field cron: sec min hour day month weekday, e.g. '0 */5 * * * *'")],
) -> str:
    """Change a Seerr job's cron schedule. Takes effect live and persists.

    Seerr rejects invalid expressions.
    """
    try:
        if len(schedule.split()) != 6:
            return out({"error": "schedule must have 6 cron fields",
                        "example": "0 */5 * * * *"})
        jobs = await api("seerr", "GET", "/api/v1/settings/jobs")
        if not any(j.get("id") == job_id for j in jobs):
            return out({"error": f"unknown job '{job_id}'",
                        "valid": [j.get("id") for j in jobs]})
        r = await api("seerr", "POST",
                      f"/api/v1/settings/jobs/{job_id}/schedule",
                      body={"schedule": schedule})
        return out({"ok": True, "id": r.get("id"),
                    "schedule": r.get("cronSchedule"),
                    "nextRun": r.get("nextExecutionTime")})
    except Exception as e:
        return err(e)


@mcp.tool(name="stack_drift", annotations={
    "title": "Library-wide drift check", "readOnlyHint": True,
    "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def stack_drift(
    limit: Annotated[int, Field(ge=1, le=500,
                                description="Max rows per category")] = 50,
) -> str:
    """Find Seerr media marked available that the arrs or Jellyfin no
    longer have.

    Reason codes: NOT-IN-ARR (removed from Radarr/Sonarr), ARR-NO-FILE
    (monitored but fileless), NOT-IN-JELLYFIN (advisory: may also mean
    missing Jellyfin provider ids). ARR reasons are authoritative.
    Fix with job_run('availability-sync').
    """
    try:
        media: list[dict] = []
        skip = 0
        while True:
            page = await api("seerr", "GET", "/api/v1/media",
                             params={"take": 100, "skip": skip})
            batch = page.get("results", [])
            media.extend(batch)
            total = (page.get("pageInfo") or {}).get("results", len(media))
            skip += len(batch)
            if not batch or skip >= total:
                break
        radarr = {m["tmdbId"]: m for m in
                  await api("radarr", "GET", "/api/v3/movie")}
        sonarr = {s["tvdbId"]: s for s in
                  await api("sonarr", "GET", "/api/v3/series")}
        jf_movies = jf_ids(await jf_all_items("Movie"))["Tmdb"]
        jf_series = jf_ids(await jf_all_items("Series"))
        movie_drift, tv_drift = [], []
        for m in media:
            if m.get("mediaType") == "movie" and m.get("status") == 5:
                t = m.get("tmdbId")
                r = radarr.get(t)
                reasons = []
                if r is None:
                    reasons.append("NOT-IN-RADARR")
                elif not r.get("hasFile"):
                    reasons.append("RADARR-NO-FILE")
                if t not in jf_movies:
                    reasons.append("NOT-IN-JELLYFIN")
                if reasons:
                    movie_drift.append(
                        {"tmdbId": t,
                         "title": (r or {}).get("title", f"tmdb:{t}"),
                         "reasons": reasons,
                         "seerrUpdated": m.get("updatedAt")})
            elif m.get("mediaType") == "tv" and m.get("status") in (4, 5):
                t = m.get("tvdbId")
                s = sonarr.get(t)
                reasons = []
                if s is None:
                    reasons.append("NOT-IN-SONARR")
                if t not in jf_series["Tvdb"] and t not in jf_series["Tmdb"]:
                    reasons.append("NOT-IN-JELLYFIN")
                if reasons:
                    tv_drift.append(
                        {"tvdbId": t,
                         "title": (s or {}).get("title", f"tvdb:{t}"),
                         "reasons": reasons,
                         "seerrUpdated": m.get("updatedAt")})
        return out({
            "seerrMediaRows": len(media),
            "movieDriftCount": len(movie_drift),
            "tvDriftCount": len(tv_drift),
            "movies": movie_drift[:limit],
            "tv": tv_drift[:limit],
            "fix": "job_run('availability-sync')"})
    except Exception as e:
        return err(e)


@mcp.tool(name="stack_health", annotations={
    "title": "One-call stack triage", "readOnlyHint": True,
    "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def stack_health(
    include_drift: Annotated[bool, Field(
        description="Include the library drift scan (slower)")] = True,
) -> str:
    """Triage the whole media stack in one call.

    Checks Radarr/Sonarr health items, Seerr jobs, Jellyfin reachability,
    NZBGet status, and (optionally) library drift. Overall is ok, warn
    (issues or drift found), or error (a service is unreachable).
    """
    services: dict[str, Any] = {}
    overall = "ok"
    for svc in ("radarr", "sonarr"):
        try:
            items = await api(svc, "GET", "/api/v3/health")
            services[svc] = {
                "reachable": True,
                "issues": [{"type": i.get("type"),
                            "message": i.get("message")} for i in items]}
            if items and overall == "ok":
                overall = "warn"
        except Exception as e:
            services[svc] = {"reachable": False, "error": str(e)}
            overall = "error"
    try:
        jobs = await api("seerr", "GET", "/api/v1/settings/jobs")
        services["seerr"] = {
            "reachable": True, "jobs": len(jobs),
            "running": [j.get("id") for j in jobs if j.get("running")]}
    except Exception as e:
        services["seerr"] = {"reachable": False, "error": str(e)}
        overall = "error"
    try:
        info = await api("jellyfin", "GET", "/System/Info/Public")
        services["jellyfin"] = {"reachable": True,
                                "version": info.get("Version")}
    except Exception as e:
        services["jellyfin"] = {"reachable": False, "error": str(e)}
        overall = "error"
    status, note = await nzbget_rpc("status")
    if status is not None:
        rate = (status.get("DownloadRateLo", 0)
                + (status.get("DownloadRateHi", 0) << 32)) \
            or status.get("DownloadRate", 0)
        services["nzbget"] = {"reachable": True,
                              "downloadRateMBs": round(rate / 1048576, 2),
                              "queueSize": status.get("QueueSize")}
    else:
        services["nzbget"] = {"reachable": False, "note": note}
    if include_drift:
        try:
            drift = json.loads(await stack_drift(limit=1))
            services["drift"] = {
                "movieDriftCount": drift.get("movieDriftCount"),
                "tvDriftCount": drift.get("tvDriftCount")}
            if ((drift.get("movieDriftCount") or 0)
                    + (drift.get("tvDriftCount") or 0)) and overall == "ok":
                overall = "warn"
        except Exception as e:
            services["drift"] = {"error": str(e)}
    return out({"overall": overall, "services": services})


# ---------------------------------------------------------------- nzbget

_nzb_conn: Optional[dict] = None


async def nzbget_conn() -> Optional[dict]:
    """NZBGet host/port from Radarr's downloadclient config (cached).

    Credentials are NOT discoverable: Radarr's API masks the password.
    They come from NZBGET_USER / NZBGET_PASS env vars instead.
    """
    global _nzb_conn
    if _nzb_conn is None:
        conn: dict = {}
        try:
            dc = await api("radarr", "GET", "/api/v3/downloadclient")
            n = next((d for d in dc if d.get("implementation") == "Nzbget"
                      and d.get("enable")), None)
            if n:
                f = {x["name"]: x.get("value") for x in n.get("fields", [])}
                if f.get("host"):
                    conn = {"host": f["host"],
                            "port": int(f.get("port") or 6789)}
        except Exception:
            pass
        _nzb_conn = conn
    return _nzb_conn or None


async def nzbget_rpc(method: str, params: Optional[list] = None):
    """Read-only NZBGet JSON-RPC. Returns (result, note); note explains
    any degradation. Never raises."""
    user = os.environ.get("NZBGET_USER", "")
    pw = os.environ.get("NZBGET_PASS", "")
    if not user or not pw:
        return None, ("NZBGet credentials not configured "
                      "(NZBGET_USER/NZBGET_PASS)")
    conn = await nzbget_conn()
    if not conn:
        return None, "no enabled NZBGet download client found in Radarr"
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
    try:
        r = await http.post(
            f"http://{conn['host']}:{conn['port']}/jsonrpc",
            json={"method": method, "params": params or [], "id": 1},
            headers={"Authorization": f"Basic {auth}"}, timeout=15.0)
        if r.status_code == 401:
            return None, "NZBGet auth failed (check NZBGET_USER/NZBGET_PASS)"
        r.raise_for_status()
        return r.json().get("result"), None
    except Exception as e:
        return None, f"NZBGet unreachable: {type(e).__name__}"


def _nzb_progress(g: dict) -> dict:
    size = g.get("FileSizeMB") or 0
    rem = g.get("RemainingSizeMB") or 0
    pct = round((size - rem) / size * 100, 1) if size else 0.0
    return {"name": g.get("NZBName"), "status": g.get("Status"),
            "progressPct": pct, "sizeMB": size, "remainingMB": rem,
            "healthPct": (g.get("Health") or 0) / 10}


# ---------------------------------------------------------------- diagnosis tools


@mcp.tool(name="queue_status", annotations={
    "title": "Download queue across the stack", "readOnlyHint": True,
    "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def queue_status(
    media_type: Annotated[str | None, Field(
        description="Optional filter: movie or tv")] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> str:
    """Active downloads in Radarr/Sonarr plus the NZBGet queue.

    Read-only observation. NZBGet progress is computed from sizes (the
    API has no Progress field); health is permille converted to percent.
    Without NZBGet credentials the arr queues still return, with a note.
    """
    try:
        rows = []
        for svc, mt in (("radarr", "movie"), ("sonarr", "tv")):
            if media_type and media_type != mt:
                continue
            try:
                q = await api(svc, "GET", "/api/v3/queue",
                              params={"page": 1, "pageSize": limit})
            except Exception:
                continue
            for r in q.get("records", []):
                rows.append({"arr": svc, "title": r.get("title"),
                             "status": r.get("status"),
                             "state": r.get("trackedDownloadState"),
                             "sizeleft": r.get("sizeleft"),
                             "timeleft": r.get("timeleft"),
                             "error": r.get("errorMessage"),
                             "downloadClient": r.get("downloadClient")})
        groups, note = await nzbget_rpc("listgroups")
        nzb = [_nzb_progress(g) for g in groups[:limit]] \
            if groups is not None else None
        return out({"arrQueue": rows, "arrCount": len(rows),
                    "nzbget": nzb, "nzbgetNote": note})
    except Exception as e:
        return err(e)


@mcp.tool(name="download_history", annotations={
    "title": "Download history for a title", "readOnlyHint": True,
    "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def download_history(
    media_type: Annotated[str, Field(pattern="^(movie|tv)$")],
    tmdb_id: Annotated[int, Field(ge=1)],
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> str:
    """Recent arr history events for one title: grabs, imports, failures.

    Newest first. Use to see what was grabbed and why an import failed.
    """
    try:
        if media_type == "movie":
            movies = await api("radarr", "GET", "/api/v3/movie")
            m = next((x for x in movies if x.get("tmdbId") == tmdb_id), None)
            if not m:
                return out({"error": "title not found in Radarr",
                            "hint": "Check the tmdbId with media_search."})
            svc, idfield, arr_id = "radarr", "movieId", m["id"]
        else:
            d = await api("seerr", "GET", f"/api/v1/tv/{tmdb_id}")
            tvdb = (d.get("mediaInfo") or {}).get("tvdbId")
            series = await api("sonarr", "GET", "/api/v3/series")
            s = next((x for x in series if x.get("tvdbId") == tvdb), None)
            if not s:
                return out({"error": "title not found in Sonarr",
                            "hint": "Check the tmdbId with media_search."})
            svc, idfield, arr_id = "sonarr", "seriesId", s["id"]
        h = await api(svc, "GET", "/api/v3/history", params={
            idfield: arr_id, "page": 1, "pageSize": limit,
            "sortKey": "date", "sortDirection": "descending"})
        events = [{"date": e.get("date"), "eventType": e.get("eventType"),
                   "sourceTitle": e.get("sourceTitle"),
                   "quality": ((e.get("quality") or {}).get("quality")
                               or {}).get("name"),
                   "languages": [l.get("name")
                                 for l in (e.get("languages") or [])]}
                  for e in h.get("records", [])]
        return out({"arr": svc, "count": len(events), "events": events})
    except Exception as e:
        return err(e)


@mcp.tool(name="request_trace", annotations={
    "title": "Trace a request through the whole stack", "readOnlyHint": True,
    "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def request_trace(
    media_type: Annotated[str, Field(pattern="^(movie|tv)$")],
    tmdb_id: Annotated[int, Field(ge=1)],
) -> str:
    """Answer "why is this request stuck?" by walking the full chain:
    Seerr -> Radarr/Sonarr -> queue -> NZBGet -> history -> Jellyfin.

    Returns facts plus a verdict: not-requested, not-in-arr, not-monitored,
    waiting-for-release, downloading, import-blocked, failed-download,
    waiting-for-import, available, available-but-drifted. Each verdict
    carries a next_step. Read-only; acts through Seerr only when you then
    call request_retry or the job tools.
    """
    try:
        try:
            d = await api("seerr", "GET", f"/api/v1/{media_type}/{tmdb_id}")
        except ApiError as e:
            if e.status == 404:
                return out({"verdict": "not-requested",
                            "next_step": "request_create if Tyler wants it"})
            raise
        mi = d.get("mediaInfo") or {}
        title = d.get("title") or d.get("name") or f"tmdb:{tmdb_id}"
        if not mi:
            return out({"verdict": "not-requested",
                        "next_step": "request_create if Tyler wants it"})
        base = {"title": title, "mediaType": media_type, "tmdbId": tmdb_id,
                "seerrStatus": MEDIA_STATUS.get(mi.get("status"))}
        if media_type == "movie":
            movies = await api("radarr", "GET", "/api/v3/movie")
            m = next((x for x in movies if x.get("tmdbId") == tmdb_id), None)
            svc, idfield = "radarr", "movieId"
            arr_id = m.get("id") if m else None
            has_file = bool(m and m.get("hasFile"))
            monitored = bool(m and m.get("monitored"))
            arr_facts = {"inArr": bool(m), "hasFile": has_file,
                         "monitored": monitored,
                         "fileAdded": ((m.get("movieFile") or {})
                                       .get("dateAdded") if m else None)}
        else:
            tvdb = mi.get("tvdbId")
            series = await api("sonarr", "GET", "/api/v3/series")
            s = next((x for x in series if x.get("tvdbId") == tvdb), None)
            svc, idfield = "sonarr", "seriesId"
            arr_id = s.get("id") if s else None
            stats = (s.get("statistics") or {}) if s else {}
            has_file = (stats.get("episodeFileCount") or 0) > 0
            monitored = bool(s and s.get("monitored"))
            arr_facts = {"inArr": bool(s), "hasFile": has_file,
                         "monitored": monitored,
                         "episodeFiles": stats.get("episodeFileCount"),
                         "episodes": stats.get("episodeCount")}
        base["arr"] = arr_facts
        if not arr_facts["inArr"]:
            return out({**base, "verdict": "not-in-arr",
                        "next_step": "check the Seerr request state; "
                                     "request_retry if it failed"})
        if has_file:
            jf = await jf_find(title,
                               tmdb=tmdb_id if media_type == "movie" else None,
                               tvdb=mi.get("tvdbId"))
            base["jellyfin"] = jf
            if mi.get("status") == 5 and jf:
                return out({**base, "verdict": "available",
                            "next_step": None})
            return out({**base, "verdict": "available-but-drifted",
                        "next_step": "job_run('radarr-scan' or "
                                     "'availability-sync'); check Jellyfin"})
        if not monitored:
            return out({**base, "verdict": "not-monitored",
                        "next_step": "enable monitoring in the arr UI"})
        q = await api(svc, "GET", "/api/v3/queue",
                      params={"page": 1, "pageSize": 100})
        entry = next((r for r in q.get("records", [])
                      if r.get(idfield) == arr_id), None)
        groups, nzb_note = await nzbget_rpc("listgroups")
        if entry:
            state = entry.get("trackedDownloadState") or entry.get("status")
            detail = entry.get("errorMessage") or "; ".join(
                x.get("message", "")
                for x in (entry.get("statusMessages") or []))
            nzb_match = None
            if groups:
                t = (entry.get("title") or "").lower()
                g = next((g for g in groups
                          if t and t in (g.get("NZBName") or "").lower()),
                         None)
                nzb_match = _nzb_progress(g) if g else None
            if state in ("importPending", "importBlocked"):
                verdict = "import-blocked"
                step = "read detail; common causes: sample file, wrong " \
                       "path, permissions. Fix in the arr, then it imports."
            elif entry.get("status") == "failed" or state == "failed":
                verdict = "failed-download"
                step = "request_retry, or check indexers/quality profile"
            else:
                verdict = "downloading"
                step = "wait; re-check queue_status for progress"
            return out({**base, "verdict": verdict, "detail": detail or None,
                        "queue": {"status": entry.get("status"),
                                  "state": state,
                                  "sizeleft": entry.get("sizeleft"),
                                  "timeleft": entry.get("timeleft")},
                        "nzbget": nzb_match, "nzbgetNote": nzb_note,
                        "next_step": step})
        h = await api(svc, "GET", "/api/v3/history", params={
            idfield: arr_id, "page": 1, "pageSize": 10,
            "sortKey": "date", "sortDirection": "descending"})
        events = h.get("records", [])
        base["recentHistory"] = [
            {"date": e.get("date"), "eventType": e.get("eventType"),
             "sourceTitle": e.get("sourceTitle")} for e in events[:5]]
        grabbed = [e for e in events if e.get("eventType") == "grabbed"]
        if grabbed:
            nzb_hist, _ = await nzbget_rpc("history", [False, 0, 20])
            fail = None
            if nzb_hist:
                t = (grabbed[0].get("sourceTitle") or "").lower()
                fail = next((x for x in nzb_hist
                             if t and t in (x.get("Name") or "").lower()
                             and str(x.get("Status", "")).startswith(
                                 "FAILURE")), None)
            if fail:
                return out({**base, "verdict": "failed-download",
                            "detail": f"NZBGet: {fail.get('Status')}",
                            "next_step": "request_retry or manual search "
                                         "in the arr"})
            return out({**base, "verdict": "waiting-for-import",
                        "next_step": "grabbed but not queued: check NZBGet "
                                     "post-processing, then the arr's "
                                     "activity tab"})
        return out({**base, "verdict": "waiting-for-release",
                    "next_step": "request_retry to re-search; if it keeps "
                                 "failing check quality profile and indexers"})
    except Exception as e:
        return err(e)


@mcp.tool(name="release_info", annotations={
    "title": "File quality and release data", "readOnlyHint": True,
    "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
async def release_info(
    media_type: Annotated[str, Field(pattern="^(movie|tv)$")],
    tmdb_id: Annotated[int, Field(ge=1)],
) -> str:
    """Current file data for replacement decisions: quality, resolution,
    codec, audio languages, size, profile. TV gives a per-season summary.

    Feed this to a comparison before grabbing a replacement; the grab
    itself must go through Seerr (request system) or the arr UI.
    """
    try:
        if media_type == "movie":
            movies = await api("radarr", "GET", "/api/v3/movie")
            m = next((x for x in movies if x.get("tmdbId") == tmdb_id), None)
            if not m:
                return out({"error": "title not found in Radarr"})
            mf = m.get("movieFile") or {}
            mi = mf.get("mediaInfo") or {}
            profile = None
            try:
                profiles = await api("radarr", "GET",
                                     "/api/v3/qualityprofile")
                p = next((x for x in profiles
                          if x.get("id") == m.get("qualityProfileId")),
                         None)
                if p:
                    profile = {"id": p.get("id"), "name": p.get("name")}
            except Exception:
                pass
            return out({
                "title": m.get("title"), "tmdbId": tmdb_id,
                "file": {"quality": ((mf.get("quality") or {})
                                     .get("quality") or {}).get("name"),
                         "resolution": mi.get("resolution"),
                         "videoCodec": mi.get("videoCodec"),
                         "audioLanguages": mi.get("audioLanguages"),
                         "sizeGB": round((mf.get("size") or 0)
                                         / 1073741824, 2),
                         "runtime": mf.get("runtime"),
                         "cutoffNotMet": mf.get("qualityCutoffNotMet"),
                         "path": mf.get("path")},
                "profile": profile})
        d = await api("seerr", "GET", f"/api/v1/tv/{tmdb_id}")
        tvdb = (d.get("mediaInfo") or {}).get("tvdbId")
        series = await api("sonarr", "GET", "/api/v3/series")
        s = next((x for x in series if x.get("tvdbId") == tvdb), None)
        if not s:
            return out({"error": "title not found in Sonarr"})
        files = await api("sonarr", "GET", "/api/v3/episodefile",
                          params={"seriesId": s["id"]})
        seasons: dict[int, dict] = {}
        for f in files:
            sn = f.get("seasonNumber", 0)
            entry = seasons.setdefault(
                sn, {"season": sn, "files": 0, "sizeGB": 0.0,
                     "qualities": {}, "audioLanguages": set()})
            entry["files"] += 1
            entry["sizeGB"] += (f.get("size") or 0) / 1073741824
            qn = ((f.get("quality") or {}).get("quality") or {}).get("name")
            if qn:
                entry["qualities"][qn] = entry["qualities"].get(qn, 0) + 1
            langs = (f.get("mediaInfo") or {}).get("audioLanguages")
            if langs:
                entry["audioLanguages"].add(langs)
        summary = []
        for sn in sorted(seasons):
            e = seasons[sn]
            e["sizeGB"] = round(e["sizeGB"], 2)
            e["audioLanguages"] = sorted(e["audioLanguages"])
            summary.append(e)
        return out({"title": s.get("title"), "tvdbId": tvdb,
                    "seasons": summary})
    except Exception as e:
        return err(e)


# ---------------------------------------------------------------- auth + app


class BearerAuth:
    """Require Authorization: Bearer <token> on everything except /health."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") != "/health":
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode()
            if not TOKEN or auth != f"Bearer {TOKEN}":
                resp = JSONResponse({"error": "unauthorized"}, status_code=401)
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def health(request: Any) -> JSONResponse:
    present = {}
    for name, path in (("seerr", f"{CONFIG_DIR}/seerr/settings.json"),
                       ("radarr", f"{CONFIG_DIR}/radarr/config.xml"),
                       ("sonarr", f"{CONFIG_DIR}/sonarr/config.xml")):
        present[name] = os.path.exists(path)
    return JSONResponse({"status": "ok", "name": "clankarr",
                         "version": VERSION, "configs": present})


def build_app() -> ASGIApp:
    app = mcp.streamable_http_app()
    app.add_route("/health", health, methods=["GET"])
    return BearerAuth(app)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("CLANKARR_TOKEN is not set; refusing to start.")
    keys.load()
    print(f"clankarr {VERSION} on :{PORT} (configs loaded)", flush=True)
    uvicorn.run(build_app(), host="0.0.0.0", port=PORT, log_level="info")
