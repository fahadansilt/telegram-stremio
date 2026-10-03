import hashlib
import unicodedata


def normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).casefold()
    return "".join(char for char in text if char.isalnum())


def series_key(title: str, year: int | None, imdb_id: str | None) -> str:
    if imdb_id:
        return imdb_id
    identity = f"{normalize(title)}:{year or ''}"
    return hashlib.sha256(identity.encode()).hexdigest()[:32]


def episode_number(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
