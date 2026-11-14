"""Characterization tests for the v1 api() helper."""
import httpx
import pytest

import clankarr


async def test_seerr_uses_api_key_header(respx_mock):
    route = respx_mock.get("http://seerr:5055/api/v1/settings/jobs").mock(
        return_value=httpx.Response(200, json=[]))
    await clankarr.api("seerr", "GET", "/api/v1/settings/jobs")
    assert route.calls[0].request.headers["X-Api-Key"] == "test-seerr-key"


async def test_jellyfin_uses_mediabrowser_header(respx_mock):
    route = respx_mock.get("http://jellyfin:8096/Users").mock(
        return_value=httpx.Response(200, json=[]))
    await clankarr.api("jellyfin", "GET", "/Users")
    auth = route.calls[0].request.headers["Authorization"]
    assert auth == 'MediaBrowser Token="test-jf-key"'


async def test_401_retries_once_with_reloaded_keys(respx_mock):
    route = respx_mock.get("http://radarr:7878/api/v3/movie").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json=[])])
    result = await clankarr.api("radarr", "GET", "/api/v3/movie")
    assert result == []
    assert route.call_count == 2


async def test_404_raises_apierror(respx_mock):
    respx_mock.get("http://seerr:5055/api/v1/movie/999").mock(
        return_value=httpx.Response(404))
    with pytest.raises(clankarr.ApiError) as ei:
        await clankarr.api("seerr", "GET", "/api/v1/movie/999")
    assert ei.value.status == 404
    assert ei.value.service == "seerr"


async def test_204_returns_none(respx_mock):
    respx_mock.post("http://seerr:5055/api/v1/request/1/approve").mock(
        return_value=httpx.Response(204))
    assert await clankarr.api(
        "seerr", "POST", "/api/v1/request/1/approve", body={}) is None


def test_err_masks_nothing_leaks_keys():
    e = clankarr.ApiError("radarr", 401, "/api/v3/movie")
    msg = clankarr.err(e)
    assert "test-radarr-key" not in msg
    assert "401" in msg
