import logging
import re
import unicodedata
from urllib.parse import quote

import httpx
from guessit import guessit

from app.database import Database

log = logging.getLogger(__name__)
IMDB = re.compile(r"\btt\d{7,10}\b")


def normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).casefold()
    return "".join(char for char in text if char.isalnum())


def parse_filename(filename: str, caption: str) -> dict | None:
    parsed = guessit(filename)
    if parsed.get("type") == "episode" or parsed.get("episode") is not None:
        return None
    title = parsed.get("title")
    if not title:
        title = next((line.strip() for line in caption.splitlines() if line.strip()), filename)
    qualities = [parsed.get(key) for key in ("screen_size", "video_codec", "source")]
    quality = " • ".join(str(item) for item in qualities if item)
    ids = set(IMDB.findall(filename + " " + caption))
    return {
        "title": str(title), "year": parsed.get("year"), "quality": quality,
        "imdb_id": next(iter(ids)) if len(ids) == 1 else None,
    }


class Metadata:
    def __init__(self, db: Database, enabled: bool):
        self.db = db
        self.enabled = enabled
        self.http = httpx.AsyncClient(timeout=10, follow_redirects=True)

    async def close(self):
        await self.http.aclose()

    async def request(self, path: str) -> dict:
        cached = await self.db.cache_get(path)
        if cached is not None:
            return cached
        try:
            response = await self.http.get(f"https://v3-cinemeta.strem.io/{path}")
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                return {}
        except (httpx.HTTPError, ValueError):
            log.warning("Cinemeta lookup unavailable; keeping Telegram metadata")
            return {}
        await self.db.cache_set(path, value)
        return value

    async def get(self, imdb_id: str) -> dict:
        if not self.enabled:
            return {}
        value = await self.request(f"meta/movie/{imdb_id}.json")
        meta = value.get("meta") or {}
        # Store descriptive fields only; playback always uses our own IDs and routes.
        return {key: meta[key] for key in (
            "name", "poster", "background", "description", "releaseInfo", "genres", "imdbRating"
        ) if key in meta}

    async def match(self, parsed: dict) -> tuple[str | None, dict]:
        imdb_id = parsed["imdb_id"]
        if imdb_id:
            return imdb_id, await self.get(imdb_id)
        if not self.enabled:
            return None, {}
        result = await self.request(
            f"catalog/movie/top/search={quote(parsed['title'], safe='')}.json"
        )
        matches = {}
        for candidate in result.get("metas", []):
            candidate_id = candidate.get("id", "")
            if not IMDB.fullmatch(candidate_id):
                continue
            if normalize(candidate.get("name", "")) != normalize(parsed["title"]):
                continue
            year = str(candidate.get("releaseInfo", candidate.get("year", "")))[:4]
            if parsed["year"] and year != str(parsed["year"]):
                continue
            matches[candidate_id] = candidate
        # Never select the first fuzzy search result or silently choose between remakes.
        if len(matches) == 1:
            imdb_id = next(iter(matches))
            return imdb_id, await self.get(imdb_id)
        return None, {}
