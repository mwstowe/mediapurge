import requests

from mediacleaner.config import get_config


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
        params={"removeFiles": str(remove_files).lower()},
        verify=False,
    )
    r.raise_for_status()
