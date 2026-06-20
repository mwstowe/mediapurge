import requests

from mediapurge.config import get_config


def _base() -> tuple[str, dict]:
    cfg = get_config()["medusa"]
    return cfg["url"].rstrip("/"), {"X-Api-Key": cfg["api_key"]}


def _get(url, headers):
    return requests.get(url, headers=headers, verify=False)


def get_all_shows() -> list[dict]:
    url, headers = _base()
    r = _get(f"{url}/api/v2/series?limit=1000", headers)
    r.raise_for_status()
    return r.json()


def get_show_by_path(path: str) -> dict | None:
    for s in get_all_shows():
        show_path = s.get("config", {}).get("location", s.get("location", ""))
        if path.startswith(show_path):
            return s
    return None


def delete_show(show_slug: str, remove_files: bool = True):
    url, headers = _base()
    r = requests.delete(
        f"{url}/api/v2/series/{show_slug}",
        headers=headers,
        json={"remove": True, "removeFiles": remove_files},
        verify=False,
    )
    r.raise_for_status()


def ignore_episode(show_slug: str, season: int, episode: int):
    """Mark an episode as Ignored and clear its quality and release info."""
    url, headers = _base()
    ep_id = f"s{season:02d}e{episode:02d}"
    r = requests.patch(
        f"{url}/api/v2/series/{show_slug}/episodes/{ep_id}",
        headers=headers,
        json={"status": 7, "quality": 0, "release": {"name": ""}},
        verify=False,
    )
    r.raise_for_status()


def refresh_show(show_slug: str):
    """Trigger a show refresh to clear stale file info."""
    url, headers = _base()
    cfg = get_config()["medusa"]
    # Extract TVDB ID from slug (e.g., "tvdb448176" -> 448176)
    tvdb_id = show_slug.replace("tvdb", "")
    r = requests.get(
        f"{url}/api/v1/{cfg['api_key']}/?cmd=show.refresh&tvdbid={tvdb_id}",
        verify=False,
    )
    r.raise_for_status()


def get_root_folders() -> list[str]:
    """Get unique root folders from Medusa's managed shows."""
    folders = set()
    for s in get_all_shows():
        path = s.get("config", {}).get("location", "")
        if path:
            # Root folder is the parent of the show folder
            parent = "/".join(path.rstrip("/").split("/")[:-1])
            folders.add(parent)
    return sorted(folders)


def add_show(tvdb_id: int, location: str, anime: bool = False, show_list: str = None, default_status: str = "Wanted"):
    """Add a show to Medusa, then patch its config (Medusa ignores config at add time)."""
    import time
    url, headers = _base()

    # Step 1: Add the show (Medusa ignores config in POST)
    r = requests.post(
        f"{url}/api/v2/series",
        headers=headers,
        json={"id": {"tvdb": tvdb_id}},
        verify=False,
    )
    r.raise_for_status()

    # Step 2: Wait for Medusa to process the add
    slug = f"tvdb{tvdb_id}"
    time.sleep(5)

    # Step 3: Patch config — set defaultEpisodeStatus to Ignored first to prevent downloads
    config_patch = {"config": {
        "location": location,
        "anime": anime,
        "defaultEpisodeStatus": "Ignored",
        "paused": True,
    }}
    if show_list:
        config_patch["config"]["showLists"] = [show_list]
    elif anime:
        config_patch["config"]["showLists"] = ["anime"]
    requests.patch(f"{url}/api/v2/series/{slug}", headers=headers, json=config_patch, verify=False)
    if anime:
        requests.patch(f"{url}/api/v2/series/{slug}", headers=headers, json={"showType": "anime"}, verify=False)

    # Step 4: Refresh to detect existing files (sets them to Downloaded)
    refresh_show(slug)
    time.sleep(5)

    # Step 5: Unpause and set the real defaultEpisodeStatus for future episodes
    requests.patch(f"{url}/api/v2/series/{slug}", headers=headers,
                   json={"config": {"defaultEpisodeStatus": default_status, "paused": False}}, verify=False)
