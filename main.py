"""
Anime Stream API
=================

A FastAPI service exposing anime search / episode-listing / stream-link
resolution, ported from the scraping logic in frostnova721/animestream
(https://github.com/frostnova721/animestream) — a Flutter app. This
service re-implements the same providers and extractors in Python:

Providers:
  - animepahe   (AnimePahe.pw, resolves via the Kwik extractor)
  - gojo        (animetsu.live, JSON API, multi-server + subtitles)
  - anizone     (anizone.to, HTML scrape)
  - anidb       (anidb.app community mirror, m3u8 via embedded script)
  - animegg     (animegg.org, HTML + embedded JS video sources)
  - anikoto     (anikototv.to, AJAX HTML fragments, resolves via Vidtube)
  - animeonsen  (animeonsen.xyz, OAuth2 client-credentials JSON API, DASH+ASS)

Extractors:
  - kwik        (used by animepahe; unpacks P.A.C.K.E.R.-packed JS to find .m3u8)
  - streamwish  (ported, not currently wired to a provider — available for reuse)
  - vidtube     (used by anikoto)

IMPORTANT: these are unofficial third-party scraping sources, not a
stable public API. Source sites can change their HTML/JSON shape or
block traffic at any time — see the /health endpoint to check live
reachability, and see the bottom of this file for troubleshooting notes.

Run:
    pip install -r requirements.txt
    uvicorn main:app --reload
"""
from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


class SearchResult(BaseModel):
    name: str
    alias: str
    imageUrl: Optional[str] = None


class EpisodeDetails(BaseModel):
    episodeLink: str
    episodeNumber: float
    thumbnail: Optional[str] = None
    episodeTitle: Optional[str] = None
    description: Optional[str] = None
    hasDub: Optional[bool] = False
    isFiller: Optional[bool] = False
    metadata: Optional[str] = None


class VideoStream(BaseModel):
    quality: str
    url: str
    server: str
    backup: bool = False
    subtitle: Optional[str] = None
    subtitleFormat: Optional[str] = None
    customHeaders: Optional[dict] = None


class ProviderHealth(BaseModel):
    provider: str
    reachable: bool
    status_code: Optional[int] = None
    error: Optional[str] = None


# --------------------------------------------------------------------------
# Shared HTTP client
# --------------------------------------------------------------------------

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko)"
)

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": DESKTOP_USER_AGENT},
            transport=httpx.AsyncHTTPTransport(retries=2),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# --------------------------------------------------------------------------
# jsunpack — port of the Dart JsUnpack class (P.A.C.K.E.R. unpacker)
# used by the kwik and streamwish extractors in the original app.
# --------------------------------------------------------------------------

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_WORD_RE = re.compile(r"\b\w+\b")
_FILTER_RE = re.compile(
    r"}\s*\('(.*)',\s*(.*?),\s*(\d+),\s*'(.*?)'\.split\('\|'\)", re.DOTALL
)
_VAR_RE = re.compile(r'var *(_\w+)=\["(.*?)"];', re.DOTALL)


def _to_base10(s: str) -> int:
    out = 0
    for char in s:
        out = out * len(_ALPHABET) + _ALPHABET.find(char)
    return out


def _filter_args(source: str):
    m = _FILTER_RE.search(source)
    if not m:
        raise ValueError("Corrupted p.a.c.k.e.r. data.")
    payload = m.group(1)
    symtab = m.group(4).split("|")
    return payload, symtab


def _replace_strings(source: str) -> str:
    m = _VAR_RE.search(source)
    if not m:
        return source
    return source[len(m.group(1)):]


def js_unpack(source: str) -> str:
    """Unpacks P.A.C.K.E.R. packed JS code."""
    payload, symtab = _filter_args(source)
    payload = payload.replace("\\\\", "\\").replace("\\'", "'")

    parts = []
    last_end = 0
    for m in _WORD_RE.finditer(payload):
        word = m.group(0)
        v = _to_base10(word)
        lookup = (symtab[v] or word) if v < len(symtab) else word
        parts.append(payload[last_end:m.start()])
        parts.append(lookup)
        last_end = m.end()
    parts.append(payload[last_end:])

    return _replace_strings("".join(parts))


# --------------------------------------------------------------------------
# Extractor: Kwik (used by AnimePahe)
# --------------------------------------------------------------------------

_KWIK_EVAL_RE = re.compile(r"eval\(function\(p,a,c,k,e,d\)")
_KWIK_SOURCE_RE = re.compile(r"const\s+source\s*=\s*'([^']+\.m3u8)'")


async def kwik_extract(
    stream_url: str, quality: Optional[str] = None, server: Optional[str] = None
) -> list[VideoStream]:
    client = get_client()
    res = await client.get(stream_url, headers={"referer": "https://animepahe.pw/"})
    res.raise_for_status()

    doc = BeautifulSoup(res.text, "lxml")
    stream_link: Optional[str] = None

    for script in doc.find_all("script"):
        html = script.decode_contents()
        if not _KWIK_EVAL_RE.search(html):
            continue
        unpacked = js_unpack(html)
        match = _KWIK_SOURCE_RE.search(unpacked)
        if match:
            stream_link = re.sub(r'{|}|"|file:', "", match.group(1))
            break

    if not stream_link:
        raise ValueError("UNABLE TO EXTRACT KWIK STREAM")

    return [
        VideoStream(
            quality=quality or "single",
            url=stream_link,
            server=server or "Kwik",
            backup=False,
            customHeaders={"referer": "https://kwik.cx/"},
        )
    ]


# --------------------------------------------------------------------------
# Extractor: StreamWish (ported for reuse; not wired to a provider here)
# --------------------------------------------------------------------------

_SW_FILE_RE = re.compile(r'file:\s*"(.*?)"')
_SW_EVAL_RE = re.compile(r"eval\(function\(p,a,c,k,e,d\)")
_SW_SOURCES_RE = re.compile(r"sources:\s*\[([\s\S]*?)\]")
_SW_TRACKS_RE = re.compile(r"tracks:\[([\s\S]*?)\]")
_SW_SUB_RE = re.compile(
    r'\{[^}]*file\s*:\s*"([^"]+)"[^}]*label\s*:\s*"English"[^}]*kind\s*:\s*"captions"',
    re.IGNORECASE | re.MULTILINE,
)
_SW_LINKS_OBJ_RE = re.compile(r"var\s+links\s*=\s*\{([\s\S]*?)\};")
_SW_LINKS_ENTRY_RE = re.compile(r'"?(\w+)"?\s*:\s*"((?:\\.|[^"\\])*)"')


async def streamwish_extract(
    stream_url: str,
    label: Optional[str] = None,
    headers_override: Optional[dict] = None,
) -> list[VideoStream]:
    if not stream_url:
        raise ValueError("ERROR: INVALID STREAM LINK")

    server_name = label or "streamwish"
    client = get_client()
    res = await client.get(stream_url)
    res.raise_for_status()

    doc = BeautifulSoup(res.text, "lxml")
    stream_link = ""
    subtitles: Optional[str] = None
    unpacked_data = ""

    for script in doc.find_all("script"):
        if stream_link:
            break
        html = script.decode_contents()

        file_match = _SW_FILE_RE.search(html)
        if file_match:
            unpacked_data = html
            stream_link = file_match.group(1)
        elif _SW_EVAL_RE.search(html):
            data = js_unpack(html)
            unpacked_data = data
            sources_match = _SW_SOURCES_RE.search(data)
            if sources_match:
                stream_link = re.sub(r'{|}|"|file:', "", sources_match.group(1))

        subtitle_match = _SW_TRACKS_RE.search(unpacked_data)
        if subtitle_match:
            sub_m = _SW_SUB_RE.search(subtitle_match.group(1))
            subtitles = sub_m.group(1) if sub_m else None

        parsed = urlparse(stream_link)
        if not parsed.scheme:
            variables = stream_link.split("||")
            links_m = _SW_LINKS_OBJ_RE.search(unpacked_data)
            extracted = (
                {k: v for k, v in _SW_LINKS_ENTRY_RE.findall(links_m.group(1))}
                if links_m
                else {}
            )
            for variable in variables:
                parts = variable.split(".")
                if len(parts) == 2 and parts[0].strip() == "links":
                    stream_link = extracted.get(parts[1].strip(), "")

    if not stream_link:
        raise ValueError(f"Couldnt get any {server_name} streams")

    subtitle_format = None
    if subtitles:
        subtitle_format = "vtt" if subtitles.endswith(".vtt") else "ass"

    headers = headers_override or {
        "Referer": stream_url,
        "Origin": f"https://{urlparse(stream_url).netloc}",
    }

    return [
        VideoStream(
            server=server_name,
            url=stream_link,
            quality="multi-quality",
            backup=False,
            subtitle=subtitles,
            subtitleFormat=subtitle_format,
            customHeaders=headers,
        )
    ]


# --------------------------------------------------------------------------
# Extractor: Vidtube (used by Anikoto)
# --------------------------------------------------------------------------


async def vidtube_extract(
    stream_url: str, quality: str = "multi-quality", server: Optional[str] = None
) -> list[VideoStream]:
    client = get_client()
    headers = {"X-Requested-With": "XMLHttpRequest"}

    parsed = urlparse(stream_url)
    res = await client.get(stream_url)
    res.raise_for_status()

    doc = BeautifulSoup(res.text, "lxml")
    player = doc.find(id="megaplay-player")
    video_id = player.get("data-id") if player else None
    if not video_id:
        raise ValueError("Failed to extract video ID from the video page.")

    stream_type = parsed.path.rstrip("/").rsplit("/", 1)[-1]

    final_res = await client.get(
        "https://vidtube.site/stream/getSourcesNew",
        params={"id": video_id, "type": stream_type},
        headers=headers,
    )
    final_res.raise_for_status()
    data = final_res.json()

    sources = data.get("sources") or {}
    tracks = [t for t in (data.get("tracks") or []) if t.get("kind") == "captions"]

    playlist = sources.get("file") if isinstance(sources, dict) else None
    if not playlist:
        raise ValueError("No video sources found.")

    sub = next((t.get("file") for t in tracks if str(t.get("lang")) == "english"), None)
    if not sub:
        sub = next((t.get("file") for t in tracks if t.get("default")), None)

    return [
        VideoStream(
            quality=quality,
            url=playlist,
            server=server or "vidtube",
            backup=False,
            subtitle=sub,
            subtitleFormat="vtt" if sub else None,
            customHeaders={
                "Referer": "https://vidtube.site/",
                "Origin": "https://vidtube.site",
            },
        )
    ]


# --------------------------------------------------------------------------
# Provider: AnimePahe
# --------------------------------------------------------------------------

_ANIMEPAHE_HEADERS = {
    "Cookie": "__ddg1=;__ddg2_=",
    "referer": "https://animepahe.pw/",
}
_ANIMEPAHE_MAX_PAGES = 6  # mirrors upstream's own cutoff for very long series


class AnimePahe:
    provider_name = "animepahe"
    base_url = "https://animepahe.pw"

    async def search(self, query: str) -> list[SearchResult]:
        query = query.replace("-", "")
        client = get_client()
        res = await client.get(
            f"{self.base_url}/api",
            params={"m": "search", "q": query},
            headers=_ANIMEPAHE_HEADERS,
        )
        res.raise_for_status()
        data = res.json()
        results = data.get("data", [])
        return [
            SearchResult(name=r["title"], alias=r["session"], imageUrl=r.get("poster"))
            for r in results
        ]

    async def get_episodes(self, session: str, dub: bool = False) -> list[EpisodeDetails]:
        client = get_client()
        base_url = f"{self.base_url}/api"
        params = {"m": "release", "id": session, "sort": "episode_asc"}

        res = await client.get(base_url, params=params, headers=_ANIMEPAHE_HEADERS)
        res.raise_for_status()
        body = res.json()

        pages = [body["data"]]
        total_pages = body.get("last_page", 1)

        for i in range(1, min(total_pages, _ANIMEPAHE_MAX_PAGES)):
            page_res = await client.get(
                base_url, params={**params, "page": i + 1}, headers=_ANIMEPAHE_HEADERS
            )
            page_res.raise_for_status()
            pages.append(page_res.json()["data"])

        flat = [item for page in pages for item in page]

        episodes: list[EpisodeDetails] = []
        n = len(flat)
        for i, item in enumerate(flat):
            episode_link = f"{self.base_url}/play/{session}/{item['session']}"
            title = item.get("title") or None
            episodes.append(
                EpisodeDetails(
                    episodeLink=episode_link,
                    episodeNumber=n - i,
                    thumbnail=item.get("snapshot"),
                    episodeTitle=title,
                    isFiller=item.get("filler", 0) != 0,
                    hasDub=item.get("audio") != "jpn",
                )
            )

        return list(reversed(episodes))

    async def get_streams(
        self, episode_url: str, dub: bool = False, metadata: Optional[str] = None
    ) -> list[VideoStream]:
        client = get_client()
        res = await client.get(episode_url, headers=_ANIMEPAHE_HEADERS)
        res.raise_for_status()

        doc = BeautifulSoup(res.text, "lxml")
        buttons = doc.select("div#resolutionMenu > button")

        candidates = []
        for btn in buttons:
            link = btn.get("data-src", "")
            text = btn.get_text()
            parts = text.split("\u00b7")
            if len(parts) < 2:
                continue
            server = parts[0].strip()
            quality = parts[1].strip()
            has_dub = "eng" in quality.split(" ")
            if dub == has_dub:
                candidates.append({"link": link, "server": server, "quality": quality})

        if not candidates:
            raise ValueError(
                "No matching streams found on the episode page "
                "(site layout may have changed, or no dub/sub variant available)"
            )

        async def resolve(entry: dict) -> list[VideoStream]:
            try:
                return await kwik_extract(
                    entry["link"], server=entry["server"], quality=entry["quality"]
                )
            except Exception:
                return []

        results = await asyncio.gather(*(resolve(c) for c in candidates))
        streams = [s for group in results for s in group]
        if not streams:
            raise ValueError("Found stream buttons but Kwik extraction failed for all of them")
        return streams


# --------------------------------------------------------------------------
# Provider: Gojo (animetsu.live)
# --------------------------------------------------------------------------

_GOJO_API_URL = "https://animetsu.live/v2/api/anime"
_GOJO_PROXY_URL = "https://swiftstream.top/proxy"
_GOJO_HEADERS = {
    "Origin": "https://animetsu.live",
    "Referer": "https://animetsu.live/",
    "User-Agent": DESKTOP_USER_AGENT,
}


def _gojo_proxied(url: Optional[str]) -> Optional[str]:
    if url and url.startswith("/"):
        return f"{_GOJO_PROXY_URL}{url}"
    return url


class Gojo:
    provider_name = "gojo"
    base_url = "https://animetsu.live"

    async def search(self, query: str) -> list[SearchResult]:
        client = get_client()
        res = await client.get(
            f"{_GOJO_API_URL}/search/", params={"query": query}, headers=_GOJO_HEADERS
        )
        res.raise_for_status()
        results = res.json().get("results", [])

        out = []
        for item in results:
            title = item["title"].get("english") or item["title"].get("romaji") or ""
            out.append(
                SearchResult(
                    name=title,
                    alias=str(item["id"]),
                    imageUrl=item.get("cover_image", {}).get("medium"),
                )
            )
        return out

    async def get_episodes(self, alias_id: str, dub: bool = False) -> list[EpisodeDetails]:
        client = get_client()
        res = await client.get(f"{_GOJO_API_URL}/eps/{alias_id}", headers=_GOJO_HEADERS)
        res.raise_for_status()
        items = res.json()

        episodes = []
        for item in items:
            ep_num = item.get("ep_num")
            episodes.append(
                EpisodeDetails(
                    episodeLink=str(alias_id),
                    episodeNumber=ep_num,
                    isFiller=bool(item.get("is_filler")),
                    thumbnail=_gojo_proxied(item.get("img")),
                    episodeTitle=item.get("name"),
                    hasDub=True,
                    metadata=str(ep_num),
                )
            )
        return episodes

    async def get_streams(
        self, ep_link: str, dub: bool = False, metadata: Optional[str] = None
    ) -> list[VideoStream]:
        if metadata is None:
            raise ValueError(
                "gojo requires the 'metadata' query param (the episode number "
                "returned by /episodes) to fetch streams"
            )

        client = get_client()
        anime_id = ep_link
        ep_num = metadata

        server_res = await client.get(
            f"{_GOJO_API_URL}/servers/{anime_id}/{ep_num}", headers=_GOJO_HEADERS
        )
        server_res.raise_for_status()
        servers = server_res.json()

        if not servers:
            raise ValueError("No servers returned for this episode")

        async def fetch_server(server: dict):
            r = await client.get(
                f"{_GOJO_API_URL}/oppai/{anime_id}/{ep_num}",
                params={"server": server["id"], "source_type": "dub" if dub else "sub"},
                headers=_GOJO_HEADERS,
            )
            r.raise_for_status()
            return server["id"], r.json()

        results = await asyncio.gather(
            *(fetch_server(s) for s in servers), return_exceptions=True
        )

        streams: list[VideoStream] = []
        for result in results:
            if isinstance(result, Exception):
                continue
            provider, data = result
            sources = data.get("sources") or []
            subtitles = data.get("subs") or []
            if not sources:
                continue

            english_sub = next(
                (s["url"] for s in subtitles if s.get("lang") == "English"), None
            )
            sub_url = english_sub or (subtitles[0]["url"] if subtitles else None)
            if sub_url and sub_url.startswith("/"):
                sub_url = f"{_GOJO_PROXY_URL}{sub_url}"

            for src in sources:
                quality = (
                    "multi-quality"
                    if (src.get("quality") or "").strip() == "master"
                    else src.get("quality")
                )
                url = src["url"]
                if url.startswith("/"):
                    url = f"{_GOJO_PROXY_URL}{url}"
                streams.append(
                    VideoStream(
                        quality=quality,
                        url=url,
                        server=str(provider),
                        backup=False,
                        subtitleFormat="vtt",
                        customHeaders=_GOJO_HEADERS,
                        subtitle=sub_url,
                    )
                )

        if not streams:
            raise ValueError("All servers returned empty source lists")
        return streams


# --------------------------------------------------------------------------
# Provider: AniZone
# --------------------------------------------------------------------------

_AZ_JSON_DATA_RE = re.compile(r"JSON\.parse\('(.+?)'\)", re.DOTALL)


def _az_extract_alpine_json(el) -> dict:
    div_data = el.get("x-data")
    if not div_data:
        raise ValueError("Couldn't find anime data")
    match = _AZ_JSON_DATA_RE.search(div_data)
    if not match:
        raise ValueError("Couldn't find anime data")
    raw = match.group(1).replace(r"\u0022", '"')
    return json.loads(raw)


class AniZone:
    provider_name = "anizone"
    base_url = "https://anizone.to"

    async def search(self, query: str) -> list[SearchResult]:
        client = get_client()
        res = await client.get(f"{self.base_url}/anime", params={"search": query})
        res.raise_for_status()

        doc = BeautifulSoup(res.text, "lxml")
        grid = doc.select_one("div.grid.grid-cols-1.gap-4")
        if grid is None:
            raise ValueError("Search results grid not found (site layout may have changed)")

        results = []
        for child in grid.find_all(recursive=False):
            data = _az_extract_alpine_json(child)
            title = data.get("1")

            a = child.find("a")
            img = child.find("img")
            if a is None or img is None:
                raise ValueError("Found null item.")

            href = a.get("href")
            src = img.get("src")
            if not href or not src or not title:
                raise ValueError("Found null image/title/url.")

            results.append(SearchResult(name=title, alias=href, imageUrl=src))

        return results

    async def get_episodes(self, alias_id: str, dub: bool = False) -> list[EpisodeDetails]:
        client = get_client()
        res = await client.get(alias_id)
        res.raise_for_status()

        doc = BeautifulSoup(res.text, "lxml")
        list_el = doc.select_one("ul.grid.grid-cols-1")
        if list_el is None:
            return []

        episodes = []
        for i, item in enumerate(list_el.find_all(recursive=False), start=1):
            data = _az_extract_alpine_json(item)
            title = data.get("1")

            a = item.find("a")
            img = item.find("img")

            episodes.append(
                EpisodeDetails(
                    episodeLink=a.get("href") if a else "",
                    episodeNumber=i,
                    thumbnail=img.get("src") if img else None,
                    episodeTitle=title,
                    isFiller=False,
                    hasDub=False,
                )
            )

        return episodes

    async def get_streams(
        self, episode_id: str, dub: bool = False, metadata: Optional[str] = None
    ) -> list[VideoStream]:
        client = get_client()
        res = await client.get(episode_id)
        res.raise_for_status()

        doc = BeautifulSoup(res.text, "lxml")
        media_player = doc.find("media-player")
        if media_player is None:
            raise ValueError("Couldnt find media player (site layout may have changed)")

        src = media_player.get("src")
        if not src:
            raise ValueError("Failed to resolve the source link")

        subs = []
        for track in media_player.find_all("track"):
            if (
                track.get("srclang") == "en"
                and track.get("kind") == "subtitles"
                and track.has_attr("default")
            ):
                subs.append({"url": track.get("src"), "type": track.get("data-type")})

        server_name_el = doc.select_one(
            ".flex.gap-2.relative.items-center.p-3.rounded-lg.text-white.bg-teal-600"
        )
        server_name = server_name_el.get_text(strip=True) if server_name_el else "single"

        first_sub = subs[0] if subs else None

        return [
            VideoStream(
                quality="multi-quality",
                url=src,
                server=server_name,
                subtitle=first_sub["url"] if first_sub else None,
                subtitleFormat=first_sub["type"] if first_sub else None,
                backup=False,
            )
        ]


# --------------------------------------------------------------------------
# Provider: AniDB (anidb.app — unofficial community mirror, not the classic
# anidb.net metadata site)
# --------------------------------------------------------------------------

_ANIDB_HEADERS = {"User-Agent": "Chrome"}
_ANIDB_M3U8_RE = re.compile(r"file:\s*'([^']+\.m3u8[^']*)'")


class AniDB:
    provider_name = "anidb"
    base_url = "https://anidb.app"

    async def search(self, query: str) -> list[SearchResult]:
        client = get_client()
        res = await client.get(
            f"{self.base_url}/browse", params={"q": query}, headers=_ANIDB_HEADERS
        )
        res.raise_for_status()

        doc = BeautifulSoup(res.text, "lxml")
        a_tags = doc.select(".anime-grid a")

        results = []
        for tag in a_tags:
            href = tag.get("href") or ""
            anime_id = href.rstrip("/").split("/")[-1] if href else None
            title_el = tag.find("p")
            img_el = tag.find("img")
            if anime_id:
                results.append(
                    SearchResult(
                        name=title_el.get_text(strip=True) if title_el else "",
                        alias=anime_id,
                        imageUrl=img_el.get("src") if img_el else None,
                    )
                )
        return results

    async def get_episodes(self, alias_id: str, dub: bool = False) -> list[EpisodeDetails]:
        client = get_client()
        anime_id = alias_id.split("-")[-1]
        res = await client.get(
            f"{self.base_url}/api/frontend/anime/{anime_id}/episodes", headers=_ANIDB_HEADERS
        )
        res.raise_for_status()
        data = res.json()

        episodes = []
        for ep in data.get("episodes") or []:
            episodes.append(
                EpisodeDetails(
                    episodeLink=str(ep["id"]),
                    episodeNumber=float(ep["number"]),
                    hasDub=dub,
                    isFiller=bool(ep.get("filler", False)),
                )
            )
        return episodes

    async def get_streams(
        self, episode_id: str, dub: bool = False, metadata: Optional[str] = None
    ) -> list[VideoStream]:
        client = get_client()
        res = await client.get(
            f"{self.base_url}/api/frontend/episode/{episode_id}/languages",
            headers=_ANIDB_HEADERS,
        )
        res.raise_for_status()
        data = res.json()

        languages = data.get("languages") or []
        if not languages:
            return []

        streams = []
        for lang in languages:
            embed_url = lang.get("embed_url")
            if not embed_url:
                continue
            try:
                embed_res = await client.get(embed_url, headers=_ANIDB_HEADERS)
                embed_res.raise_for_status()
            except Exception:
                continue

            doc = BeautifulSoup(embed_res.text, "lxml")
            scripts = doc.select("body script")
            if len(scripts) <= 1:
                continue

            match = _ANIDB_M3U8_RE.search(scripts[1].get_text())
            if not match:
                continue

            streams.append(
                VideoStream(
                    url=match.group(1),
                    quality=str(lang.get("name") or "default"),
                    server="Anidb",
                    backup=False,
                )
            )

        return streams


# --------------------------------------------------------------------------
# Provider: AnimEgg
# --------------------------------------------------------------------------

_AG_VIDEOSOURCES_RE = re.compile(r"var\s+videoSources\s*=\s*(\[[\s\S]*?\]);", re.MULTILINE)
_AG_KEY_RE = re.compile(r"(\w+):")


class AnimEgg:
    provider_name = "animegg"
    base_url = "https://www.animegg.org"

    async def search(self, query: str) -> list[SearchResult]:
        # NOTE: the upstream Dart app called a JSON endpoint at
        # /search/auto/?q= — that endpoint no longer exists. The live site
        # now only has a server-rendered HTML results page at /search/?q=,
        # confirmed against the current site on 2026-07-20.
        client = get_client()
        res = await client.get(f"{self.base_url}/search/", params={"q": query})
        res.raise_for_status()

        doc = BeautifulSoup(res.text, "lxml")
        results = []
        for a in doc.select("a[href^='/series/']"):
            href = a.get("href") or ""
            if not href:
                continue
            # The link's own text is "<Title>Episodes: N Alt Titles : ...Status : ...";
            # the title is the text before "Episodes:" — img thumbnails aren't part of
            # this markup (results are a plain text link list, no per-result image).
            full_text = a.get_text(" ", strip=True)
            title = full_text.split("Episodes:")[0].strip()
            if not title:
                continue
            alias = f"{self.base_url}{href}" if href.startswith("/") else href
            results.append(SearchResult(name=title, alias=alias, imageUrl=None))
        return results

    async def get_episodes(self, alias_id: str, dub: bool = False) -> list[EpisodeDetails]:
        client = get_client()
        res = await client.get(alias_id)
        res.raise_for_status()

        doc = BeautifulSoup(res.text, "lxml")
        tab = doc.find(class_="newmanga")
        if tab is None:
            raise ValueError("Couldnt find the episodes section.")

        children = tab.find_all(recursive=False)
        episodes = []
        n = len(children)
        for i in range(n - 1, -1, -1):
            elem = children[i]
            div = elem.find(recursive=False)
            a = div.find("a") if div else None
            if div is None or a is None:
                raise ValueError("Couldnt find the element with the episode infos")

            title_el = div.find(class_="anititle")
            title = title_el.get_text(strip=True) if title_el else None
            url = self.base_url + a.get("href", "")

            episodes.append(
                EpisodeDetails(
                    episodeNumber=n - i,
                    episodeLink=url,
                    episodeTitle=(title or "").replace("[Filler]", "") or None,
                    hasDub=div.select_one(".btn-xs.btn-dubbed") is not None,
                    isFiller=(title or "").startswith("[Filler]"),
                )
            )
        return episodes

    async def get_streams(
        self, episode_id: str, dub: bool = False, metadata: Optional[str] = None
    ) -> list[VideoStream]:
        client = get_client()
        res = await client.get(episode_id)
        res.raise_for_status()

        doc = BeautifulSoup(res.text, "lxml")
        videos = doc.find(id="videos")
        if videos is None:
            raise ValueError("Couldnt find streams!")

        streams: list[VideoStream] = []

        for li in videos.find_all(recursive=False):
            for item in li.find_all(recursive=False):
                is_sub = item.get("data-version") == "subbed"
                if dub != (not is_sub):
                    continue

                video_id = item.get("data-id")
                if not video_id:
                    continue
                stream_page_url = f"{self.base_url}/embed/{video_id}"

                stream_res = await client.get(stream_page_url)
                stream_res.raise_for_status()
                stream_doc = BeautifulSoup(stream_res.text, "lxml")

                for script in stream_doc.find_all("script"):
                    body = script.decode_contents()
                    match = _AG_VIDEOSOURCES_RE.search(body)
                    if not match:
                        continue

                    cleaned = _AG_KEY_RE.sub(r'"\1":', match.group(1)).replace("'", '"')
                    try:
                        source_list = json.loads(cleaned)
                    except json.JSONDecodeError:
                        continue

                    for src in source_list:
                        streams.append(
                            VideoStream(
                                quality=str(src.get("label")),
                                url=self.base_url + src.get("file", ""),
                                server="AnimEgg",
                                backup=bool(src.get("isBk", False)),
                                customHeaders={"referer": stream_page_url},
                            )
                        )
                    break

        return streams


# --------------------------------------------------------------------------
# Provider: Anikoto
# --------------------------------------------------------------------------

_ANIKOTO_HEADERS = {
    "Referer": "https://anikototv.to/",
    "X-Requested-With": "XMLHttpRequest",
}


class Anikoto:
    provider_name = "anikoto"
    base_url = "https://anikototv.to"
    _ajax_url = "https://anikototv.to/ajax"
    _mapper_url = "https://mapper.nekostream.site"

    async def search(self, query: str) -> list[SearchResult]:
        client = get_client()
        res = await client.get(
            f"{self._ajax_url}/anime/search", params={"keyword": query}, headers=_ANIKOTO_HEADERS
        )
        res.raise_for_status()
        html_string = res.json().get("result", {}).get("html")
        if not html_string:
            raise ValueError("Failed to fetch search results")

        doc = BeautifulSoup(html_string, "lxml")
        items_container = doc.select_one("div.scaff.items")
        if items_container is None:
            raise ValueError("Failed to parse search results. No items found.")

        results = []
        for item in items_container.find_all(recursive=False):
            title_el = item.select_one(".name.d-title")
            link = item.get("href")
            img_el = item.find("img")
            if title_el and link:
                results.append(
                    SearchResult(
                        name=title_el.get_text(strip=True),
                        alias=link,
                        imageUrl=img_el.get("src") if img_el else None,
                    )
                )
        return results

    async def get_episodes(self, alias_id: str, dub: bool = False) -> list[EpisodeDetails]:
        client = get_client()
        res = await client.get(alias_id, headers=_ANIKOTO_HEADERS)
        res.raise_for_status()

        doc = BeautifulSoup(res.text, "lxml")
        watch_main = doc.find(id="watch-main")
        anime_id = watch_main.get("data-id", "").strip() if watch_main else None
        if not anime_id:
            raise ValueError("Failed to fetch anime episode link. No data-id found.")

        ep_list_res = await client.get(
            f"{self._ajax_url}/episode/list/{anime_id}", params={"vrf": ""}, headers=_ANIKOTO_HEADERS
        )
        ep_list_res.raise_for_status()
        ep_list_html = ep_list_res.json().get("result")
        if not ep_list_html:
            raise ValueError("Failed to fetch episode list. No HTML found.")

        ep_doc = BeautifulSoup(ep_list_html, "lxml")
        episode_items = ep_doc.select_one("div.episodes")
        if episode_items is None:
            raise ValueError("Failed to parse episode list. No episodes found.")

        episodes = []
        for rng in episode_items.find_all(recursive=False):
            for ep in rng.find_all(recursive=False):
                a = ep.find("a")
                if a is None:
                    continue

                episode_link = a.get("data-ids", "").strip() or None
                data_mal = (a.get("data-mal") or "").strip()
                episode_number = (a.get("data-num") or "").strip()
                dub_available = (a.get("data-dub") or "").strip() == "1"
                is_filler = "filler" in (a.get("class") or [])

                if a.get("href") and episode_number:
                    episodes.append(
                        EpisodeDetails(
                            episodeLink=episode_link or "",
                            episodeNumber=float(episode_number),
                            episodeTitle=(ep.get("title") or "").strip() or None,
                            hasDub=dub_available,
                            isFiller=is_filler,
                            metadata=f"{data_mal}-{episode_number}",
                        )
                    )
        return episodes

    async def _get_kiwi_stream_id(self, mal_id: str, ep: int) -> dict:
        import time

        client = get_client()
        ts = int(time.time())
        res = await client.get(
            f"{self._mapper_url}/api/mal/{mal_id}/{ep}/{ts}", headers=_ANIKOTO_HEADERS
        )
        res.raise_for_status()
        data = res.json()
        return data.get("Kiwi-Stream-") or {}

    async def get_streams(
        self, episode_id: str, dub: bool = False, metadata: Optional[str] = None
    ) -> list[VideoStream]:
        client = get_client()

        kiwi_task = None
        if metadata:
            split = metadata.split("-")
            if len(split) == 2:
                try:
                    ep = int(split[1])
                except ValueError:
                    ep = 0
                if ep > 0:
                    kiwi_task = asyncio.ensure_future(self._get_kiwi_stream_id(split[0], ep))

        server_list_res = await client.get(
            f"{self._ajax_url}/server/list", params={"servers": episode_id}, headers=_ANIKOTO_HEADERS
        )
        server_list_res.raise_for_status()
        server_list_html = server_list_res.json().get("result")
        if not server_list_html:
            raise ValueError("Failed to fetch server list. No HTML found.")

        doc = BeautifulSoup(server_list_html, "lxml")
        groups = doc.select("div.servers")

        servers = []
        for group in groups:
            grp_name = group.contents[0].get_text(strip=True) if group.contents else None
            ul = group.find("ul")
            if ul is None:
                continue

            is_dub = "dub" in (grp_name or "").lower()
            for item in ul.find_all(recursive=False):
                if is_dub != dub:
                    continue
                servers.append(
                    {
                        "srv_name": item.get_text(strip=True),
                        "link_id": (item.get("data-link-id") or "").strip(),
                        "group_name": grp_name,
                    }
                )

            if kiwi_task is not None:
                kiwi_data = await kiwi_task
                kiwi_task = None  # only consume once, mirrors upstream's placement in the loop
                if kiwi_data:
                    sub = kiwi_data.get("sub") or {}
                    servers.append(
                        {"srv_name": "Kiwi", "link_id": sub.get("url"), "group_name": "Kiwi"}
                    )

        streams: list[VideoStream] = []
        for server in servers:
            link_id = server.get("link_id")
            if not link_id:
                continue

            try:
                server_res = await client.get(
                    f"{self._ajax_url}/server", params={"get": link_id}, headers=_ANIKOTO_HEADERS
                )
                server_res.raise_for_status()
                stream_url = (server_res.json().get("result") or {}).get("url", "").strip()
            except Exception:
                continue

            if not stream_url:
                continue

            host = urlparse(stream_url).netloc.lower().split(".")[0]
            try:
                if host == "vidtube":
                    streams.extend(
                        await vidtube_extract(stream_url, server=server.get("srv_name"))
                    )
                # else: no extractor available for this host, matching upstream behaviour
            except Exception:
                continue

        return streams


# --------------------------------------------------------------------------
# Provider: AnimeOnsen
# --------------------------------------------------------------------------
# Client credentials below are the same public ones shipped in the
# upstream open-source app (credited there to the Aniyomi extensions
# project) — not a secret introduced by this file.

_AO_AUTH_URL = "https://auth.animeonsen.xyz/oauth/token"
_AO_CLIENT_ID = "f296be26-28b5-4358-b5a1-6259575e23b7"
_AO_CLIENT_SECRET = "349038c4157d0480784753841217270c3c5b35f4281eaee029de21cb04084235"

_ao_token: Optional[str] = None
_ao_token_expiry: float = 0.0
_ao_token_lock = asyncio.Lock()


async def _ao_get_token() -> str:
    global _ao_token, _ao_token_expiry
    import time

    async with _ao_token_lock:
        if _ao_token and time.time() < _ao_token_expiry - 60:
            return _ao_token

        client = get_client()
        res = await client.post(
            _AO_AUTH_URL,
            data={
                "client_id": _AO_CLIENT_ID,
                "client_secret": _AO_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
        )
        if res.status_code != 200:
            raise ValueError("couldnt generate AO token")

        data = res.json()
        _ao_token = data["access_token"]
        _ao_token_expiry = time.time() + data["expires_in"]
        return _ao_token


class AnimeOnsen:
    provider_name = "animeonsen"
    base_url = "https://www.animeonsen.xyz"

    async def search(self, query: str) -> list[SearchResult]:
        query = query.replace("-", "")
        token = await _ao_get_token()
        client = get_client()
        res = await client.get(
            f"https://api.animeonsen.xyz/v4/search/{query}",
            headers={"Authorization": f"Bearer {token}"},
        )
        res.raise_for_status()
        data = res.json()

        results = []
        for item in data.get("result") or []:
            results.append(
                SearchResult(
                    name=item.get("content_title_en") or item.get("content_title"),
                    alias=item["content_id"],
                    imageUrl=f"https://api.animeonsen.xyz/v4/image/210x300/{item['content_id']}",
                )
            )
        return results

    async def get_episodes(self, alias_id: str, dub: bool = False) -> list[EpisodeDetails]:
        token = await _ao_get_token()
        client = get_client()
        res = await client.get(
            f"https://api.animeonsen.xyz/v4/content/{alias_id}/episodes",
            headers={"Authorization": f"Bearer {token}"},
        )
        res.raise_for_status()
        data = res.json()

        episodes = []
        for i, key in enumerate(data.keys(), start=1):
            title = (data[key] or {}).get("contentTitle_episode_en")
            episodes.append(
                EpisodeDetails(
                    episodeLink=f"{key}+{alias_id}",
                    episodeNumber=float(key) if key.replace(".", "", 1).isdigit() else i,
                    episodeTitle=title or None,
                )
            )
        return episodes

    async def get_streams(
        self, episode_id: str, dub: bool = False, metadata: Optional[str] = None
    ) -> list[VideoStream]:
        episode_number, anime_id = episode_id.split("+", 1)
        manifest_url = (
            f"https://cdn.animeonsen.xyz/video/mp4-dash/{anime_id}/{episode_number}/manifest.mpd"
        )
        subtitle_url = (
            f"https://api.animeonsen.xyz/v4/subtitles/{anime_id}/en-US/{episode_number}"
        )
        return [
            VideoStream(
                quality="single",
                url=manifest_url,
                server="animeonsen",
                backup=False,
                subtitle=subtitle_url,
                subtitleFormat="ass",
                customHeaders={"Referer": "https://www.animeonsen.xyz/"},
            )
        ]


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

PROVIDERS = {
    "animepahe": AnimePahe(),
    "gojo": Gojo(),
    "anizone": AniZone(),
    "anidb": AniDB(),
    "animegg": AnimEgg(),
    "anikoto": Anikoto(),
    "animeonsen": AnimeOnsen(),
}


def get_provider(name: str):
    provider = PROVIDERS.get(name.lower())
    if provider is None:
        available = ", ".join(PROVIDERS)
        raise KeyError(f"Unknown provider '{name}'. Available: {available}")
    return provider


# --------------------------------------------------------------------------
# AniList (metadata source for the website — trending grid, search,
# cover art, synopsis. Not used for streaming; that's still the
# providers above. Proxied server-side to avoid relying on AniList's
# CORS policy for browser fetches.)
# --------------------------------------------------------------------------

_ANILIST_URL = "https://graphql.anilist.co"

_ANILIST_MEDIA_FIELDS = """
    id
    title { romaji english native }
    coverImage { large color }
    bannerImage
    description(asHtml: false)
    genres
    averageScore
    episodes
    status
    format
    seasonYear
"""

_ANILIST_TRENDING_QUERY = f"""
query ($page: Int, $perPage: Int) {{
  Page(page: $page, perPage: $perPage) {{
    media(sort: TRENDING_DESC, type: ANIME) {{
      {_ANILIST_MEDIA_FIELDS}
    }}
  }}
}}
"""

_ANILIST_POPULAR_QUERY = f"""
query ($page: Int, $perPage: Int) {{
  Page(page: $page, perPage: $perPage) {{
    media(sort: POPULARITY_DESC, type: ANIME) {{
      {_ANILIST_MEDIA_FIELDS}
    }}
  }}
}}
"""

_ANILIST_SEARCH_QUERY = f"""
query ($search: String, $page: Int, $perPage: Int) {{
  Page(page: $page, perPage: $perPage) {{
    media(search: $search, type: ANIME) {{
      {_ANILIST_MEDIA_FIELDS}
    }}
  }}
}}
"""

_ANILIST_MEDIA_BY_ID_QUERY = f"""
query ($id: Int) {{
  Media(id: $id, type: ANIME) {{
    {_ANILIST_MEDIA_FIELDS}
  }}
}}
"""


async def _anilist_query(query: str, variables: dict) -> dict:
    client = get_client()
    res = await client.post(
        _ANILIST_URL,
        json={"query": query, "variables": variables},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    res.raise_for_status()
    data = res.json()
    if "errors" in data:
        raise ValueError(data["errors"])
    return data["data"]


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_client()


app = FastAPI(
    title="Anime Stream API",
    description=(
        "Unofficial anime search/episode/stream-link API. Scraping logic "
        "ported from frostnova721/animestream."
    ),
    version="1.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/watch/{anilist_id}", response_class=HTMLResponse)
async def watch(request: Request, anilist_id: int):
    return templates.TemplateResponse(
        request, "watch.html", {"anilist_id": anilist_id}
    )


@app.get("/api")
async def api_root():
    return {
        "name": "Anime Stream API",
        "providers": list(PROVIDERS.keys()),
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/providers")
async def list_providers():
    return {"providers": list(PROVIDERS.keys())}


@app.get("/health", response_model=list[ProviderHealth])
async def health():
    """Pings each provider's base URL so you can verify live reachability
    from wherever this is actually deployed (scraping targets can be
    blocked/down independent of this app's own code)."""
    client = get_client()

    async def check(name: str, provider) -> ProviderHealth:
        try:
            res = await client.get(provider.base_url, timeout=10.0)
            # Only 2xx/3xx count as "reachable" — a 403/503 usually means an
            # anti-bot layer (or, in a sandboxed environment, an egress
            # proxy) is blocking the request, which is functionally the
            # same as unreachable for this app's purposes.
            return ProviderHealth(
                provider=name, reachable=res.status_code < 400, status_code=res.status_code
            )
        except Exception as e:
            return ProviderHealth(provider=name, reachable=False, error=str(e))

    return await asyncio.gather(*(check(n, p) for n, p in PROVIDERS.items()))


@app.get("/anilist/trending")
async def anilist_trending(page: int = 1, per_page: int = 24):
    try:
        data = await _anilist_query(
            _ANILIST_TRENDING_QUERY, {"page": page, "perPage": per_page}
        )
        return data["Page"]["media"]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AniList request failed: {e}")


@app.get("/anilist/popular")
async def anilist_popular(page: int = 1, per_page: int = 24):
    try:
        data = await _anilist_query(
            _ANILIST_POPULAR_QUERY, {"page": page, "perPage": per_page}
        )
        return data["Page"]["media"]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AniList request failed: {e}")


@app.get("/anilist/search")
async def anilist_search(q: str = Query(..., min_length=1), page: int = 1, per_page: int = 24):
    try:
        data = await _anilist_query(
            _ANILIST_SEARCH_QUERY, {"search": q, "page": page, "perPage": per_page}
        )
        return data["Page"]["media"]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AniList request failed: {e}")


@app.get("/anilist/anime/{anilist_id}")
async def anilist_anime(anilist_id: int):
    try:
        data = await _anilist_query(_ANILIST_MEDIA_BY_ID_QUERY, {"id": anilist_id})
        media = data.get("Media")
        if media is None:
            raise HTTPException(status_code=404, detail="Anime not found on AniList")
        return media
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AniList request failed: {e}")


@app.get("/{provider}/search", response_model=list[SearchResult])
async def search(provider: str, q: str = Query(..., min_length=1)):
    try:
        p = get_provider(provider)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        return await p.search(q)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"[{provider}] search failed: {e}")


@app.get("/{provider}/episodes", response_model=list[EpisodeDetails])
async def get_episodes(provider: str, alias: str = Query(...), dub: bool = False):
    try:
        p = get_provider(provider)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        return await p.get_episodes(alias, dub=dub)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"[{provider}] fetching episodes failed: {e}")


@app.get("/{provider}/streams", response_model=list[VideoStream])
async def get_streams(
    provider: str,
    episode: str = Query(..., description="episodeLink from /episodes"),
    dub: bool = False,
    metadata: Optional[str] = Query(
        None, description="metadata field from /episodes (required by gojo)"
    ),
):
    try:
        p = get_provider(provider)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        return await p.get_streams(episode, dub=dub, metadata=metadata)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"[{provider}] fetching streams failed: {e}")


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------
# Render (and most PaaS hosts) run your start command as a plain process
# and expect it to bind to $PORT and keep running in the foreground. With
# no server bootstrap here, `python main.py` would just define `app` and
# exit immediately — which is exactly the "Application exited early" seen
# in Render's logs. This starts uvicorn directly so `python main.py` works
# as a start command as-is.
if __name__ == "__main__":
    import os

    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)


# --------------------------------------------------------------------------
# Troubleshooting notes
# --------------------------------------------------------------------------
# - Check GET /health first. A provider showing reachable=false means the
#   *site* is unreachable from your network/host, not a bug in this file.
# - animepahe is behind an anti-bot layer (DDoS-Guard) on some networks;
#   if search/episodes 502 with a 403, you may need to run this from a
#   residential IP or add your own solved-cookie handling.
# - gojo (animetsu.live) is the most API-like and least likely to break
#   from markup changes, since it's JSON end-to-end.
# - anizone parses Alpine.js x-data JSON blobs embedded in HTML; if the
#   site redesigns its listing page, the CSS selectors in AniZone will
#   need updating (search for "grid.grid-cols-1" in this file).
# - anidb.app is a fan-run mirror site (not the classic anidb.net), can
#   go down independently of the others.
# - animegg's video source list is written as loose JS (unquoted keys,
#   single-quoted strings) inside a <script> tag; it's regex-cleaned into
#   valid JSON before parsing, which is brittle if the page changes format.
# - anikoto resolves streams through whatever host the server list returns;
#   this port only implements the Vidtube host (matching what upstream
#   actually wires up — mewcdn/Kwik is commented out there too). Other
#   hosts return an empty stream list rather than erroring.
# - animeonsen requires an OAuth2 token (client-credentials grant); this
#   file fetches and caches it in-memory per process, refreshing ~60s
#   before expiry. On Render's free tier the process can sleep/restart,
#   which just means a fresh token is fetched on the next request — no
#   action needed.
#
# Dub vs Sub:
#   Pass dub=true/false as a query param on /episodes and /streams.
#   Provider behavior differs because upstream sources differ:
#     - animepahe: real per-episode sub/dub split, filtered by an "eng"
#       marker in the quality label.
#     - gojo: dub/sub is a stream-fetch-time toggle (source_type=dub|sub),
#       not a separate episode list — hasDub is always true, use the dub
#       param on /streams.
#     - anizone: sub-only upstream (hasDub always false).
#     - anidb: no real dub filtering upstream; hasDub just echoes back
#       whatever you passed in.
#     - animegg: real per-server dub/sub split via data-version="subbed".
#     - anikoto: real per-server dub/sub split via a "dub" server group
#       name, plus an optional "Kiwi" stream mixed in either way.
#     - animeonsen: sub-only upstream, dub param has no effect.
#
# Render deploy settings:
#   Build command:  pip install -r requirements.txt
#   Start command:  python main.py
#   (or, equivalently: uvicorn main:app --host 0.0.0.0 --port $PORT)
# Render injects PORT automatically — don't hardcode a port in either
# command above.
