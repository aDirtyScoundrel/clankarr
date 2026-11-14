"""M5: release_info (file quality / audio / size data)."""
import json

import httpx

import clankarr


def _body(result: str) -> dict:
    return json.loads(result)


async def test_release_info_movie(respx_mock, shapes):
    movie = shapes["radarr_movie"]
    respx_mock.get("http://radarr:7878/api/v3/movie").mock(
        return_value=httpx.Response(200, json=[movie]))
    respx_mock.get("http://radarr:7878/api/v3/qualityprofile").mock(
        return_value=httpx.Response(200, json=[
            {"id": movie["qualityProfileId"], "name": "HD-1080p"}]))
    r = _body(await clankarr.release_info(media_type="movie",
                                          tmdb_id=movie["tmdbId"]))
    mf = movie["movieFile"]
    assert r["file"]["quality"] == mf["quality"]["quality"]["name"]
    assert r["file"]["sizeGB"] == round(mf["size"] / 1073741824, 2)
    assert r["profile"]["name"] == "HD-1080p"
    assert r["file"]["audioLanguages"] == mf["mediaInfo"]["audioLanguages"]


async def test_release_info_movie_missing(respx_mock):
    respx_mock.get("http://radarr:7878/api/v3/movie").mock(
        return_value=httpx.Response(200, json=[]))
    r = _body(await clankarr.release_info(media_type="movie", tmdb_id=1))
    assert "error" in r


async def test_release_info_tv_season_summary(respx_mock, shapes):
    series = shapes["sonarr_series"]
    tvdb = series["tvdbId"]
    respx_mock.get(f"http://seerr:5055/api/v1/tv/99999").mock(
        return_value=httpx.Response(200, json={
            "name": series["title"], "mediaInfo": {"tvdbId": tvdb}}))
    respx_mock.get("http://sonarr:8989/api/v3/series").mock(
        return_value=httpx.Response(200, json=[series]))
    respx_mock.get("http://sonarr:8989/api/v3/episodefile").mock(
        return_value=httpx.Response(200, json=[
            {"seasonNumber": 1, "size": 1073741824,
             "quality": {"quality": {"name": "HDTV-1080p"}},
             "mediaInfo": {"audioLanguages": "English"}},
            {"seasonNumber": 1, "size": 2147483648,
             "quality": {"quality": {"name": "Bluray-1080p"}},
             "mediaInfo": {"audioLanguages": "English / German"}},
            {"seasonNumber": 2, "size": 1073741824,
             "quality": {"quality": {"name": "HDTV-1080p"}},
             "mediaInfo": {"audioLanguages": "English"}}]))
    r = _body(await clankarr.release_info(media_type="tv", tmdb_id=99999))
    assert r["seasons"][0]["season"] == 1
    assert r["seasons"][0]["files"] == 2
    assert r["seasons"][0]["qualities"] == {"HDTV-1080p": 1,
                                            "Bluray-1080p": 1}
    assert r["seasons"][1]["season"] == 2
