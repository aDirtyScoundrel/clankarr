"""M6: notification health (list + test, URL masking)."""
import json

import httpx

import clankarr

AGENTS = ("discord", "slack", "telegram", "pushbullet", "pushover",
          "email", "webpush", "webhook", "gotify", "ntfy")


def _body(result: str) -> dict:
    return json.loads(result)


def _mock_agents(respx_mock, webhook_enabled=True):
    for a in AGENTS:
        cfg = {"enabled": False, "types": 0, "options": {}}
        if a == "webhook" and webhook_enabled:
            cfg = {"enabled": True, "types": 123,
                   "options": {"webhookUrl":
                               "https://hooks.example.com/api/SECRET123"}}
        respx_mock.get(f"http://seerr:5055/api/v1/settings/notifications/{a}"
                       ).mock(return_value=httpx.Response(200, json=cfg))


async def test_notifications_list_masks_urls(respx_mock):
    _mock_agents(respx_mock)
    raw = await clankarr.notifications_list()
    r = _body(raw)
    assert "SECRET123" not in raw
    wh = next(a for a in r["agents"] if a["agent"] == "webhook")
    assert wh["enabled"] is True
    assert wh["webhookHost"] == "hooks.example.com"
    assert r["enabledCount"] == 1


async def test_notifications_test_sends_saved_config(respx_mock):
    _mock_agents(respx_mock)
    route = respx_mock.post(
        "http://seerr:5055/api/v1/settings/notifications/webhook/test"
    ).mock(return_value=httpx.Response(204))
    r = _body(await clankarr.notifications_test(agent="webhook"))
    assert r["ok"] is True
    sent = json.loads(route.calls[0].request.content)
    assert sent["options"]["webhookUrl"].startswith("https://")


async def test_notifications_test_failure(respx_mock):
    _mock_agents(respx_mock)
    respx_mock.post(
        "http://seerr:5055/api/v1/settings/notifications/webhook/test"
    ).mock(return_value=httpx.Response(
        500, json={"message": "Failed to send webhook notification."}))
    r = _body(await clankarr.notifications_test(agent="webhook"))
    assert "error" in r


async def test_notifications_test_unknown_agent(respx_mock):
    r = _body(await clankarr.notifications_test(agent="carrier-pigeon"))
    assert "error" in r
    assert len(respx_mock.calls) == 0
