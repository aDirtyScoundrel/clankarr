"""Shared test harness: fake config tree, live-shape fixtures, HTTP mocks."""
import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "live_shapes.json"

# Environment must be set BEFORE clankarr is imported (module-level reads).
os.environ["CLANKARR_TOKEN"] = "test-token"
os.environ["CLANKARR_CONFIG_DIR"] = "/tmp/clankarr-test-config"
os.environ["SEERR_URL"] = "http://seerr:5055"
os.environ["RADARR_URL"] = "http://radarr:7878"
os.environ["SONARR_URL"] = "http://sonarr:8989"
os.environ["JELLYFIN_URL"] = "http://jellyfin:8096"
os.environ.pop("NZBGET_USER", None)
os.environ.pop("NZBGET_PASS", None)

_cfg = pathlib.Path(os.environ["CLANKARR_CONFIG_DIR"])
(_cfg / "seerr").mkdir(parents=True, exist_ok=True)
(_cfg / "radarr").mkdir(parents=True, exist_ok=True)
(_cfg / "sonarr").mkdir(parents=True, exist_ok=True)
(_cfg / "seerr" / "settings.json").write_text(json.dumps(
    {"main": {"apiKey": "test-seerr-key"},
     "jellyfin": {"apiKey": "test-jf-key"}}))
for name in ("radarr", "sonarr"):
    (_cfg / name / "config.xml").write_text(
        f"<Config><ApiKey>test-{name}-key</ApiKey></Config>")

sys.path.insert(0, str(ROOT))
import clankarr  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_keys():
    clankarr.keys._cache.clear()
    clankarr.keys._mtimes.clear()
    yield


@pytest.fixture(scope="session")
def shapes():
    return json.loads(FIXTURES.read_text())


@pytest.fixture
def mock_api(respx_mock):
    """respx router pre-bound to the four service base URLs."""
    return respx_mock
