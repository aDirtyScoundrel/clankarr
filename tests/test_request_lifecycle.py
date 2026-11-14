"""M1: request lifecycle tools (approve/decline/retry/delete, profile option)."""
import json

import httpx

import clankarr


def _body(result: str) -> dict:
    return json.loads(result)


async def test_approve(respx_mock):
    route = respx_mock.post("http://seerr:5055/api/v1/request/5/approve").mock(
        return_value=httpx.Response(200, json={"id": 5, "status": 2}))
    r = _body(await clankarr.request_approve(request_id=5))
    assert route.called
    assert r["ok"] is True
    assert r["status"] == "approved"


async def test_decline(respx_mock):
    respx_mock.post("http://seerr:5055/api/v1/request/5/decline").mock(
        return_value=httpx.Response(200, json={"id": 5, "status": 3}))
    r = _body(await clankarr.request_decline(request_id=5))
    assert r["ok"] is True
    assert r["status"] == "declined"


async def test_retry(respx_mock):
    respx_mock.post("http://seerr:5055/api/v1/request/5/retry").mock(
        return_value=httpx.Response(200, json={"id": 5, "status": 2}))
    r = _body(await clankarr.request_retry(request_id=5))
    assert r["ok"] is True


async def test_delete_request_record_only(respx_mock):
    route = respx_mock.delete("http://seerr:5055/api/v1/request/5").mock(
        return_value=httpx.Response(204))
    r = _body(await clankarr.request_delete(request_id=5))
    assert route.called
    assert r["ok"] is True
    # must NOT touch any media/file endpoint
    assert all("/media/" not in str(c.request.url)
               for c in respx_mock.calls)


async def test_approve_404_actionable(respx_mock):
    respx_mock.post("http://seerr:5055/api/v1/request/999/approve").mock(
        return_value=httpx.Response(404))
    r = _body(await clankarr.request_approve(request_id=999))
    assert "error" in r
    assert "hint" in r


async def test_request_create_passes_profile(respx_mock, shapes):
    media = shapes["seerr_media"]
    media["mediaInfo"]["status"] = 1  # unknown -> requestable
    media["mediaInfo"]["requests"] = []
    respx_mock.get("http://seerr:5055/api/v1/movie/131631").mock(
        return_value=httpx.Response(200, json=media))
    route = respx_mock.post("http://seerr:5055/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 99, "status": 2,
                                               "type": "movie"}))
    r = _body(await clankarr.request_create(
        media_type="movie", tmdb_id=131631, profile_id=4))
    assert r["ok"] is True
    sent = json.loads(route.calls[0].request.content)
    assert sent["profileId"] == 4
    assert sent["mediaId"] == 131631


async def test_request_create_no_profile_omits_field(respx_mock, shapes):
    media = shapes["seerr_media"]
    media["mediaInfo"]["status"] = 1
    media["mediaInfo"]["requests"] = []
    respx_mock.get("http://seerr:5055/api/v1/movie/131631").mock(
        return_value=httpx.Response(200, json=media))
    route = respx_mock.post("http://seerr:5055/api/v1/request").mock(
        return_value=httpx.Response(201, json={"id": 99, "status": 2,
                                               "type": "movie"}))
    await clankarr.request_create(media_type="movie", tmdb_id=131631)
    sent = json.loads(route.calls[0].request.content)
    assert "profileId" not in sent
