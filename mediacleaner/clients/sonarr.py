import requests

from mediacleaner.config import get_config


def _base() -> tuple[str, dict]:
    cfg = get_config()["sonarr"]
    return cfg["url"].rstrip("/"), {"X-Api-Key": cfg["api_key"]}


def get_all_series() -> list[dict]:
    url, headers = _base()
    r = requests.get(f"{url}/api/v3/series", headers=headers)
    r.raise_for_status()
    return r.json()


def get_episode_files(series_id: int) -> list[dict]:
    url, headers = _base()
    r = requests.get(f"{url}/api/v3/episodefile", headers=headers, params={"seriesId": series_id})
    r.raise_for_status()
    return r.json()


def delete_episode_file(episode_file_id: int):
    """Delete the file — Sonarr automatically unmonitors the episode."""
    url, headers = _base()
    r = requests.delete(f"{url}/api/v3/episodefile/{episode_file_id}", headers=headers)
    r.raise_for_status()


def get_series_by_path(path: str) -> dict | None:
    for s in get_all_series():
        if path.startswith(s["path"]):
            return s
    return None


def delete_series(series_id: int, delete_files: bool = True):
    url, headers = _base()
    r = requests.delete(
        f"{url}/api/v3/series/{series_id}",
        headers=headers,
        params={"deleteFiles": str(delete_files).lower()},
    )
    r.raise_for_status()


def unmonitor_season(series_id: int, season_number: int):
    """Unmonitor a specific season so Sonarr won't re-download it."""
    url, headers = _base()
    r = requests.get(f"{url}/api/v3/series/{series_id}", headers=headers)
    r.raise_for_status()
    series = r.json()
    for season in series.get("seasons", []):
        if season["seasonNumber"] == season_number:
            season["monitored"] = False
    r = requests.put(f"{url}/api/v3/series/{series_id}", headers=headers, json=series)
    r.raise_for_status()


def get_episodes(series_id: int) -> list[dict]:
    url, headers = _base()
    r = requests.get(f"{url}/api/v3/episode", headers=headers, params={"seriesId": series_id})
    r.raise_for_status()
    return r.json()


def unmonitor_episodes(episode_ids: list[int]):
    """Unmonitor specific episodes so Sonarr won't re-download them."""
    url, headers = _base()
    r = requests.put(f"{url}/api/v3/episode/monitor", headers=headers,
                     json={"episodeIds": episode_ids, "monitored": False})
    r.raise_for_status()
