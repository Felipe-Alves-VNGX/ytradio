"""Formato único das faixas e coleções, e a normalização das respostas do ytmusicapi."""
import re
from typing import Literal, TypedDict


class Track(TypedDict):
    kind: Literal["track"]
    id: str  # videoId
    title: str
    artist: str
    album: str
    dur: int  # segundos
    art: str  # URL da capa


class Collection(TypedDict):
    """Linha de playlist ou álbum (na tela inicial e nas listas)."""
    kind: Literal["playlist", "album"]
    id: str
    title: str
    artist: str


Row = Track | Collection


def parse_len(txt) -> int:
    if not txt or not isinstance(txt, str):
        return 0
    try:
        parts = [int(p) for p in txt.split(":")]
    except ValueError:
        return 0
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs


def big_art(url: str) -> str:
    return re.sub(r"=w\d+-h\d+[^/]*$", "=w544-h544-l90-rj", url) if "googleusercontent" in url else url


def norm(t: dict) -> Track | None:
    """Normaliza faixas de busca/playlist/rádio num formato único."""
    vid = t.get("videoId")
    if not vid or t.get("isAvailable") is False:
        return None
    artists = t.get("artists") or ([{"name": t["author"]}] if t.get("author") else [])
    album = t.get("album")
    album = album.get("name") if isinstance(album, dict) else (album or "")
    thumbs = t.get("thumbnail") or t.get("thumbnails") or []
    art = max(thumbs, key=lambda x: x.get("width", 0))["url"] if thumbs else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    secs = t.get("duration_seconds") or parse_len(t.get("length") or t.get("duration"))
    return {
        "kind": "track",
        "id": vid,
        "title": t.get("title") or "?",
        "artist": ", ".join(a["name"] for a in artists if a.get("name")),
        "album": album or "",
        "dur": secs,
        "art": big_art(art),
    }
