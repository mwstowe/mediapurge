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
