"""M2: queue_status, download_history, request_trace, NZBGet client."""
import json

import httpx
import pytest

import clankarr


def _body(result: str) -> dict:
    return json.loads(result)


@pytest.fixture(autouse=True)
def _reset_caches():
    clankarr._jf_uid = None
    clankarr._nzb_conn = None
    yield


def _mock_jf(respx_mock, present: bool, shapes):
    respx_mock.get("http://jellyfin:8096/Users").mock(
        return_value=httpx.Response(200, json=[
            {"Id": "u1", "Policy": {"IsAdministrator": True}}]))
    items = [{"Name": "The Hunger Games: Mockingjay - Part 1",
              "Type": "Movie", "ProviderIds": {"Tmdb": "131631"},
              "DateCreated": "2021-05-10T21:39:25"}] if present else []
    respx_mock.get(
        url__startswith="http://jellyfin:8096/Users/u1/Items").mock(
        return_value=httpx.Response(200, json={"Items": items,
                                               "TotalRecordCount": len(items)}))


def _mock_trace(respx_mock, shapes, *, seerr_status=3, has_file=False,
                queue=None, history=None, jf_present=False, monitored=True):
    media = json.loads(json.dumps(shapes["seerr_media"]))
    media["mediaInfo"]["status"] = seerr_status
    respx_mock.get("http://seerr:5055/api/v1/movie/131631").mock(
        return_value=httpx.Response(200, json=media))
    movie = json.loads(json.dumps(shapes["radarr_movie"]))
    movie["tmdbId"] = 131631
    movie["hasFile"] = has_file
    movie["monitored"] = monitored
    if not has_file:
        movie["movieFile"] = None
    respx_mock.get("http://radarr:7878/api/v3/movie").mock(
        return_value=httpx.Response(200, json=[movie]))
    for r in queue or []:
        r["movieId"] = movie["id"]
    respx_mock.get("http://radarr:7878/api/v3/queue").mock(
        return_value=httpx.Response(200, json={
            "records": queue or [], "totalRecords": len(queue or [])}))
    respx_mock.get("http://radarr:7878/api/v3/history").mock(
        return_value=httpx.Response(200, json={
            "records": history or [], "totalRecords": len(history or [])}))
    _mock_jf(respx_mock, jf_present, shapes)
    return movie


async def test_trace_available(respx_mock, shapes):
    _mock_trace(respx_mock, shapes, seerr_status=5, has_file=True,
                jf_present=True)
    r = _body(await clankarr.request_trace(media_type="movie", tmdb_id=131631))
    assert r["verdict"] == "available"


async def test_trace_not_requested(respx_mock):
    respx_mock.get("http://seerr:5055/api/v1/movie/131631").mock(
        return_value=httpx.Response(404))
    r = _body(await clankarr.request_trace(media_type="movie", tmdb_id=131631))
    assert r["verdict"] == "not-requested"


async def test_trace_downloading(respx_mock, shapes):
    q = {"title": "Mockingjay", "status": "downloading",
         "trackedDownloadState": "downloading", "movieId": 634,
         "sizeleft": 1000, "timeleft": "00:10:00", "downloadId": "abc",
         "downloadClient": "NZBGet", "errorMessage": None}
    _mock_trace(respx_mock, shapes, queue=[q])
    r = _body(await clankarr.request_trace(media_type="movie", tmdb_id=131631))
    assert r["verdict"] == "downloading"


async def test_trace_import_blocked(respx_mock, shapes):
    q = {"title": "Mockingjay", "status": "completed",
         "trackedDownloadState": "importPending", "movieId": 634,
         "sizeleft": 0, "timeleft": None, "downloadId": "abc",
         "downloadClient": "NZBGet",
         "errorMessage": "Import failed, path does not exist"}
    _mock_trace(respx_mock, shapes, queue=[q])
    r = _body(await clankarr.request_trace(media_type="movie", tmdb_id=131631))
    assert r["verdict"] == "import-blocked"
    assert "Import failed" in r["detail"]


async def test_trace_waiting_for_release(respx_mock, shapes):
    _mock_trace(respx_mock, shapes, queue=[], history=[])
    r = _body(await clankarr.request_trace(media_type="movie", tmdb_id=131631))
    assert r["verdict"] == "waiting-for-release"


async def test_trace_drifted(respx_mock, shapes):
    _mock_trace(respx_mock, shapes, seerr_status=3, has_file=True,
                jf_present=True)
    r = _body(await clankarr.request_trace(media_type="movie", tmdb_id=131631))
    assert r["verdict"] == "available-but-drifted"


async def test_trace_not_monitored(respx_mock, shapes):
    _mock_trace(respx_mock, shapes, monitored=False)
    r = _body(await clankarr.request_trace(media_type="movie", tmdb_id=131631))
    assert r["verdict"] == "not-monitored"


async def test_queue_status_degrades_without_nzbget_creds(respx_mock, shapes):
    respx_mock.get("http://radarr:7878/api/v3/queue").mock(
        return_value=httpx.Response(200, json=shapes["radarr_queue"]))
    respx_mock.get("http://sonarr:8989/api/v3/queue").mock(
        return_value=httpx.Response(200, json={"records": [],
                                               "totalRecords": 0}))
    r = _body(await clankarr.queue_status())
    assert len(r["arrQueue"]) == len(shapes["radarr_queue"]["records"])
    assert r["nzbget"] is None
    assert "credentials" in r["nzbgetNote"]


async def test_queue_status_with_nzbget(respx_mock, shapes, monkeypatch):
    monkeypatch.setenv("NZBGET_USER", "u")
    monkeypatch.setenv("NZBGET_PASS", "p")
    respx_mock.get("http://radarr:7878/api/v3/queue").mock(
        return_value=httpx.Response(200, json={"records": [],
                                               "totalRecords": 0}))
    respx_mock.get("http://sonarr:8989/api/v3/queue").mock(
        return_value=httpx.Response(200, json={"records": [],
                                               "totalRecords": 0}))
    respx_mock.get("http://radarr:7878/api/v3/downloadclient").mock(
        return_value=httpx.Response(200, json=shapes["radarr_downloadclient"]))
    route = respx_mock.post("http://10.0.0.1:6789/jsonrpc").mock(
        return_value=httpx.Response(200, json={"result": [
            {"NZBName": "Test.Release", "Status": "DOWNLOADING",
             "FileSizeMB": 1000, "RemainingSizeMB": 250}]}))
    r = _body(await clankarr.queue_status())
    assert route.called
    assert r["nzbget"][0]["progressPct"] == 75.0
    assert r["nzbgetNote"] is None


async def test_download_history_filters_by_movie(respx_mock, shapes):
    movie = shapes["radarr_movie"]
    respx_mock.get("http://radarr:7878/api/v3/movie").mock(
        return_value=httpx.Response(200, json=[movie]))
    route = respx_mock.get("http://radarr:7878/api/v3/history").mock(
        return_value=httpx.Response(200, json=shapes["radarr_history"]))
    r = _body(await clankarr.download_history(
        media_type="movie", tmdb_id=movie["tmdbId"]))
    assert route.called
    assert "movieId" in str(route.calls[0].request.url)
    assert len(r["events"]) == len(shapes["radarr_history"]["records"])
