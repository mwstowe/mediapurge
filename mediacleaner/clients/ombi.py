import requests

from mediacleaner.config import get_config


def _base() -> tuple[str, dict]:
    cfg = get_config()["ombi"]
    return cfg["url"].rstrip("/"), {"ApiKey": cfg["api_key"]}


def get_movie_requests() -> list[dict]:
    url, headers = _base()
    r = requests.get(f"{url}/api/v1/Request/movie", headers=headers)
    r.raise_for_status()
    return r.json()


def get_tv_requests() -> list[dict]:
    url, headers = _base()
    r = requests.get(f"{url}/api/v1/Request/tv", headers=headers)
    r.raise_for_status()
    return r.json()


def delete_movie_request(request_id: int):
    url, headers = _base()
    r = requests.delete(f"{url}/api/v1/Request/movie/{request_id}", headers=headers)
    r.raise_for_status()


def delete_tv_request(request_id: int):
    url, headers = _base()
    r = requests.delete(f"{url}/api/v1/Request/tv/{request_id}", headers=headers)
    r.raise_for_status()


def cleanup_for_title(title: str):
    """Remove any Ombi requests matching the given title."""
    for req in get_movie_requests():
        if req.get("title", "").lower() == title.lower():
            delete_movie_request(req["id"])
    for req in get_tv_requests():
        if req.get("title", "").lower() == title.lower():
            delete_tv_request(req["id"])


def approve_managed_requests(plex_ids: dict):
    """Mark Ombi requests as available when media exists in Plex, matched by TVDB/IMDB/TMDB IDs.
    plex_ids should be: {"tvdb": set(), "imdb": set(), "tmdb": set()}
    """
    url, headers = _base()
    approved = []

    for req in get_movie_requests():
        if req.get("available"):
            continue
        imdb = req.get("imdbId", "")
        tmdb = req.get("theMovieDbId")
        if (imdb and imdb in plex_ids["imdb"]) or (tmdb and tmdb in plex_ids["tmdb"]):
            r = requests.post(f"{url}/api/v1/Request/movie/available", headers=headers, json={"id": req["id"]})
            if r.status_code == 200:
                approved.append(req["title"])

    for req in get_tv_requests():
        tvdb = req.get("tvDbId")
        imdb = req.get("imdbId", "")
        if not ((tvdb and tvdb in plex_ids["tvdb"]) or (imdb and imdb in plex_ids["imdb"])):
            continue
        for child in req.get("childRequests", []):
            if not child.get("available"):
                r = requests.post(f"{url}/api/v1/Request/tv/available", headers=headers, json={"id": child["id"]})
                if r.status_code == 200:
                    approved.append(req["title"])
                break

    return approved
