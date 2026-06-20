import requests

from mediapurge.config import get_config


def _base() -> tuple[str, dict]:
    cfg = get_config()["radarr"]
    return cfg["url"].rstrip("/"), {"X-Api-Key": cfg["api_key"]}


def get_all_movies() -> list[dict]:
    url, headers = _base()
    r = requests.get(f"{url}/api/v3/movie", headers=headers)
    r.raise_for_status()
    return r.json()


def get_movie_by_path(path: str) -> dict | None:
    for m in get_all_movies():
        if path.startswith(m.get("path", "")):
            return m
    return None


def delete_movie(movie_id: int, delete_files: bool = True):
    url, headers = _base()
    r = requests.delete(
        f"{url}/api/v3/movie/{movie_id}",
        headers=headers,
        params={"deleteFiles": str(delete_files).lower()},
    )
    r.raise_for_status()
