"""
Anime Stream API
=================

A FastAPI service exposing anime search / episode-listing / stream-link
resolution, ported from the scraping logic in frostnova721/animestream
(https://github.com/frostnova721/animestream) — a Flutter app. This
service re-implements the same providers and extractors in Python:

Providers:
  - animepahe  (AnimePahe.pw, resolves via the Kwik extractor)
  - gojo       (animetsu.live, JSON API, multi-server + subtitles)
  - anizone    (anizone.to, HTML scrape)

Extractors:
  - kwik        (used by animepahe; unpacks P.A.C.K.E.R.-packed JS to find .m3u8)
  - streamwish  (ported, not currently wired to a provider — available for reuse)

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
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
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
# Registry
# --------------------------------------------------------------------------

PROVIDERS = {
    "animepahe": AnimePahe(),
    "gojo": Gojo(),
    "anizone": AniZone(),
}


def get_provider(name: str):
    provider = PROVIDERS.get(name.lower())
    if provider is None:
        available = ", ".join(PROVIDERS)
        raise KeyError(f"Unknown provider '{name}'. Available: {available}")
    return provider


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
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
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
#
# Render deploy settings:
#   Build command:  pip install -r requirements.txt
#   Start command:  python main.py
#   (or, equivalently: uvicorn main:app --host 0.0.0.0 --port $PORT)
# Render injects PORT automatically — don't hardcode a port in either
# command above.
