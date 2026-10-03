import json
import re
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.metadata import IMDB


async def fresh_addon_response(response: Response):
    # Catalogs can be requested before the background scan finds any videos.
    # Never let that initial empty response become a cached catalog or stream list.
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"


router = APIRouter(dependencies=[Depends(fresh_addon_response)])
LOCAL_ID = re.compile(r"tg:(-\d+):(\d+)")


def local_id(record: dict) -> str:
    return f"tg:{record['chat_id']}:{record['message_id']}"


def meta_for(record: dict) -> dict:
    meta = json.loads(record["metadata"])
    meta.update({"id": local_id(record), "type": "movie"})
    meta.setdefault("name", record["title"])
    meta.setdefault("description", record["caption"] or record["filename"])
    if record["year"]:
        meta.setdefault("releaseInfo", str(record["year"]))
    # Never inherit Cinemeta's default IMDb video ID for our custom catalog items.
    meta["behaviorHints"] = {"defaultVideoId": local_id(record)}
    return meta


@router.get("/manifest.json")
async def manifest(request: Request):
    return {
        "id": "org.telegram.movies", "version": "1.0.2",
        "name": request.app.state.settings.addon_name,
        "description": "Play original videos from your selected Telegram channels and groups.",
        "types": ["movie"],
        "resources": [
            "catalog", {"name": "meta", "types": ["movie"], "idPrefixes": ["tg:"]},
            {"name": "stream", "types": ["movie"], "idPrefixes": ["tg:", "tt"]},
        ],
        "catalogs": [{"type": "movie", "id": "telegram", "name": "Telegram Movies",
                      "extra": [{"name": "search", "isRequired": False}, {"name": "skip"}]}],
    }


@router.get("/catalog/{media_type}/{catalog_id}.json")
@router.get("/catalog/{media_type}/{catalog_id}/{extra:path}.json")
async def catalog(request: Request, media_type: str, catalog_id: str, extra: str = ""):
    if media_type != "movie" or catalog_id != "telegram":
        return {"metas": []}
    # Parse the encoded path so escaped '&', '=', '+', '%' and '/' in titles
    # remain values rather than becoming separators after ASGI path decoding.
    raw_path = request.scope.get("raw_path", b"").decode("ascii")
    raw_extra = raw_path.rsplit("/", 1)[-1][:-5] if extra else ""
    params = dict(parse_qsl(raw_extra, keep_blank_values=True))
    try:
        skip = int(params.get("skip", "0"))
        if skip < 0:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(400, "skip must be a non-negative integer") from exc
    records = await request.app.state.db.catalog(
        request.app.state.telegram.allowed_chats, params.get("search", ""), skip
    )
    return {"metas": [meta_for(record) for record in records]}


async def lookup_local(request: Request, item_id: str):
    match = LOCAL_ID.fullmatch(item_id)
    if not match:
        return None
    chat_id, message_id = map(int, match.groups())
    if chat_id not in request.app.state.telegram.allowed_chats:
        return None
    return await request.app.state.db.get_file(chat_id, message_id)


@router.get("/meta/{media_type}/{item_id}.json")
async def meta(request: Request, media_type: str, item_id: str):
    record = await lookup_local(request, item_id) if media_type == "movie" else None
    if record is None:
        raise HTTPException(404, "Movie not found")
    return {"meta": meta_for(record)}


@router.get("/stream/{media_type}/{item_id}.json")
async def stream(request: Request, media_type: str, item_id: str):
    if media_type != "movie":
        return {"streams": []}
    db = request.app.state.db
    allowed = request.app.state.telegram.allowed_chats
    if IMDB.fullmatch(item_id):
        records = await db.by_imdb(item_id, allowed)
    else:
        record = await lookup_local(request, item_id)
        if not record:
            return {"streams": []}
        records = await db.by_imdb(record["imdb_id"], allowed) if record["imdb_id"] else [record]
    settings = request.app.state.settings
    streams = []
    # Prefer the highest labeled resolution; file size breaks ties only.
    def rank(record):
        resolution = re.search(r"(\d{3,4})[pi]", record["quality"])
        return (int(resolution[1]) if resolution else 0, record["size"])

    for record in sorted(records, key=rank, reverse=True):
        description = (
            f"{record['filename']}\n{record['quality']} • {record['size'] / 1024**3:.2f} GiB"
        )
        # Stremio treats title as an alias for description. Sending both causes
        # a duplicate-field parse error, even when their values are identical.
        streams.append({
            "name": "Telegram", "title": description,
            "url": f"{settings.public_base_url}{settings.route_prefix}/video/"
                   f"{record['chat_id']}/{record['message_id']}",
            "behaviorHints": {
                "notWebReady": True, "filename": record["filename"], "videoSize": record["size"],
            },
        })
    return {"streams": streams}
