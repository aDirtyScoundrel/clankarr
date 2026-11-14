"""M3: stack_health triage."""
import json

import httpx

import clankarr


def _body(result: str) -> dict:
    return json.loads(result)


def _mock_healthy(respx_mock, shapes):
    respx_mock.get("http://radarr:7878/api/v3/health").mock(
        return_value=httpx.Response(200, json=[]))
    respx_mock.get("http://sonarr:8989/api/v3/health").mock(
        return_value=httpx.Response(200, json=[]))
    respx_mock.get("http://seerr:5055/api/v1/settings/jobs").mock(
        return_value=httpx.Response(200, json=shapes["seerr_jobs"]))
    respx_mock.get("http://jellyfin:8096/System/Info/Public").mock(
        return_value=httpx.Response(200, json={"Version": "12.4.0"}))


async def test_health_all_ok(respx_mock, shapes):
    _mock_healthy(respx_mock, shapes)
    r = _body(await clankarr.stack_health(include_drift=False))
    assert r["overall"] == "ok"
    assert r["services"]["radarr"]["reachable"] is True
    assert r["services"]["jellyfin"]["version"] == "12.4.0"


async def test_health_service_down(respx_mock, shapes):
    _mock_healthy(respx_mock, shapes)
    respx_mock.get("http://radarr:7878/api/v3/health").mock(
        side_effect=httpx.ConnectError("refused"))
    r = _body(await clankarr.stack_health(include_drift=False))
    assert r["overall"] == "error"
    assert r["services"]["radarr"]["reachable"] is False


async def test_health_warnings(respx_mock, shapes):
    _mock_healthy(respx_mock, shapes)
    respx_mock.get("http://radarr:7878/api/v3/health").mock(
        return_value=httpx.Response(200, json=shapes["radarr_health"]))
    r = _body(await clankarr.stack_health(include_drift=False))
    assert r["overall"] == "warn"
    assert len(r["services"]["radarr"]["issues"]) == len(shapes["radarr_health"])


async def test_health_nzbget_note_without_creds(respx_mock, shapes):
    _mock_healthy(respx_mock, shapes)
    r = _body(await clankarr.stack_health(include_drift=False))
    assert "credentials" in r["services"]["nzbget"]["note"]
