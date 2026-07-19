import time

import requests

from mediapurge.config import get_config

_cache = {"series": None, "series_time": 0}


def _base() -> tuple[str, dict]:
    cfg = get_config()["sonarr"]
    return cfg["url"].rstrip("/"), {"X-Api-Key": cfg["api_key"]}


def get_all_series() -> list[dict]:
    now = time.time()
    if _cache["series"] is not None and (now - _cache["series_time"]) < 60:
        return _cache["series"]
    url, headers = _base()
    r = requests.get(f"{url}/api/v3/series", headers=headers)
    r.raise_for_status()
    _cache["series"] = r.json()
    _cache["series_time"] = now
    return _cache["series"]


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
    _cache["series"] = None


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


def get_root_folders() -> list[str]:
    url, headers = _base()
    r = requests.get(f"{url}/api/v3/rootfolder", headers=headers)
    r.raise_for_status()
    return [f["path"] for f in r.json()]


def move_series(series_id: int, new_root_folder: str):
    """Move a series to a new root folder."""
    url, headers = _base()
    r = requests.get(f"{url}/api/v3/series/{series_id}", headers=headers)
    r.raise_for_status()
    series = r.json()
    old_path = series["path"]
    show_folder = old_path.rstrip("/").split("/")[-1]
    series["path"] = f"{new_root_folder.rstrip('/')}/{show_folder}"
    series["rootFolderPath"] = new_root_folder
    r = requests.put(f"{url}/api/v3/series/{series_id}?moveFiles=true", headers=headers, json=series)
    r.raise_for_status()


def add_series(tvdb_id: int, title: str, root_folder: str):
    """Add a series to Sonarr."""
    url, headers = _base()
    # Lookup the series first
    r = requests.get(f"{url}/api/v3/series/lookup?term=tvdb:{tvdb_id}", headers=headers)
    r.raise_for_status()
    results = r.json()
    if not results:
        raise ValueError(f"Series tvdb:{tvdb_id} not found in Sonarr lookup")
    series = results[0]
    series["rootFolderPath"] = root_folder
    series["monitored"] = True
    series["addOptions"] = {"searchForMissingEpisodes": False}
    # Use first available quality profile if not set
    if not series.get("qualityProfileId"):
        qr = requests.get(f"{url}/api/v3/qualityprofile", headers=headers)
        if qr.status_code == 200 and qr.json():
            series["qualityProfileId"] = qr.json()[0]["id"]
    r = requests.post(f"{url}/api/v3/series", headers=headers, json=series)
    r.raise_for_status()
    return r.json()["id"]


def rescan_series(series_id: int):
    """Trigger a disk scan for a series so Sonarr detects existing files."""
    url, headers = _base()
    r = requests.post(f"{url}/api/v3/command", headers=headers,
                      json={"name": "RescanSeries", "seriesId": series_id})
    r.raise_for_status()


def unmonitor_series(series_id: int):
    """Unmonitor an entire series so Sonarr won't search for anything."""
    url, headers = _base()
    r = requests.get(f"{url}/api/v3/series/{series_id}", headers=headers)
    r.raise_for_status()
    series = r.json()
    series["monitored"] = False
    r = requests.put(f"{url}/api/v3/series/{series_id}", headers=headers, json=series)
    r.raise_for_status()


def rename_series(series_id: int):
    """Rename all files in a series to match Sonarr's naming convention."""
    url, headers = _base()
    r = requests.post(f"{url}/api/v3/command", headers=headers,
                      json={"name": "RenameSeries", "seriesIds": [series_id]})
    r.raise_for_status()


def manual_import(series_id: int, files: list[dict]):
    """Manually import files into specific episodes.
    files: [{"path": str, "seriesId": int, "seasonNumber": int, "episodeIds": [int], "quality": ..., "languages": ...}]
    """
    url, headers = _base()
    r = requests.post(f"{url}/api/v3/command", headers=headers,
                      json={"name": "ManualImport", "importMode": "auto", "files": files})
    r.raise_for_status()
