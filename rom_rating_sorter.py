#!/usr/bin/env python3
"""
Rank games, movies, or TV folders by Metacritic/OpenCritic scores.

The script stores its cache in the scanned folder by default, so repeated runs do
not keep hitting public sites. Destructive operations are dry-run unless --yes is
provided.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import difflib
import html
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


CACHE_NAME = ".rom_rating_cache.json"
SYSTEM_CACHE_NAME = "metacritic_platforms.json"
METACRITIC_BASE_URL = "https://www.metacritic.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DEFAULT_EXTENSIONS = (
    ".nds,.3ds,.cia,.gba,.gb,.gbc,.nes,.sfc,.smc,.n64,.z64,.v64,.iso,.chd,"
    ".cue,.bin,.gdi,.cdi,.rvz,.wbfs,.wua,.xci,.nsp,.zip,.7z,.rar,"
    ".mkv,.mp4,.m4v,.avi,.mov,.wmv,.webm"
)
CACHE_WRITE_LOCK = threading.Lock()
DEFAULT_PLATFORM_CACHE_DAYS = 30


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


METACRITIC_GAME_PLATFORMS = (
    "3ds",
    "dreamcast",
    "game-boy-advance",
    "gamecube",
    "meta-quest",
    "mobile",
    "nintendo-64",
    "nintendo-ds",
    "nintendo-switch",
    "nintendo-switch-2",
    "pc",
    "ps-vita",
    "ps1",
    "ps2",
    "ps3",
    "ps4",
    "ps5",
    "psp",
    "wii",
    "wii-u",
    "xbox",
    "xbox-360",
    "xbox-one",
    "xbox-series-x",
)

PLATFORM_ALIASES = {
    "playstation 2": "ps2",
    "playstation 3": "ps3",
    "playstation 4": "ps4",
    "playstation 5": "ps5",
    "playstation portable": "psp",
    "playstation vita": "ps-vita",
    "playstation 1": "ps1",
    "ps vita": "ps-vita",
    "psx": "ps1",
    "ps1": "ps1",
    "nintendo switch 2": "nintendo-switch-2",
    "nintendo switch": "nintendo-switch",
    "nintendo gamecube": "gamecube",
    "nintendo 64": "nintendo-64",
    "nintendo ds": "nintendo-ds",
    "ds": "nintendo-ds",
    "nintendo 3ds": "3ds",
    "3ds": "3ds",
    "game boy advance": "game-boy-advance",
    "gba": "game-boy-advance",
    "gamecube": "gamecube",
    "wii": "wii",
    "wii u": "wii-u",
    "switch": "nintendo-switch",
    "playstation": "ps1",
    "ps2": "ps2",
    "ps3": "ps3",
    "ps4": "ps4",
    "ps5": "ps5",
    "psp": "psp",
    "xbox": "xbox",
    "xbox 360": "xbox-360",
    "xbox one": "xbox-one",
    "xbox series": "xbox-series-x",
    "xbox series x": "xbox-series-x",
    "xbox series s": "xbox-series-x",
    "dreamcast": "dreamcast",
    "pc": "pc",
    "mobile": "mobile",
    "meta quest": "meta-quest",
}

MEDIA_CONTEXT_ALIASES = {
    "movies": "movie",
    "movie": "movie",
    "films": "movie",
    "film": "movie",
    "tv shows": "tv",
    "tv show": "tv",
    "television": "tv",
    "series": "tv",
    "shows": "tv",
    "games": "game",
    "roms": "game",
}


@dataclasses.dataclass
class Rating:
    title: str
    path: str
    kind: str = "game"
    platform: str | None = None
    metacritic: int | None = None
    metacritic_url: str | None = None
    opencritic: int | None = None
    opencritic_url: str | None = None
    combined: float | None = None
    status: str = "ok"
    updated_at: float = dataclasses.field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Rating":
        allowed = {field.name for field in dataclasses.fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})

    def calculate(self) -> None:
        scores = [s for s in (self.metacritic, self.opencritic) if s is not None]
        self.combined = round(sum(scores) / len(scores), 2) if scores else None


@dataclasses.dataclass
class PlatformRating:
    title: str
    slug: str
    metacritic: int
    metacritic_url: str
    updated_at: float | None = None
    user_score: float | None = None
    user_score_url: str | None = None
    user_score_updated_at: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlatformRating":
        return cls(
            title=str(data["title"]),
            slug=str(data["slug"]),
            metacritic=int(data["metacritic"]),
            metacritic_url=str(data["metacritic_url"]),
            updated_at=float(data["updated_at"]) if data.get("updated_at") is not None else None,
            user_score=float(data["user_score"]) if data.get("user_score") is not None else None,
            user_score_url=str(data["user_score_url"]) if data.get("user_score_url") is not None else None,
            user_score_updated_at=(
                float(data["user_score_updated_at"]) if data.get("user_score_updated_at") is not None else None
            ),
        )


def fetch_text(url: str, timeout: float) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def clean_title(path: Path) -> str:
    name = path.stem if path.suffix else path.name
    name = re.sub(r"\[[^\]]*\]", " ", name)
    name = re.sub(r"\([^)]*\)", " ", name)
    name = re.sub(r"\bS\d{1,2}E\d{1,2}\b.*$", " ", name, flags=re.I)
    name = re.sub(r"\b\d{3,4}p\b", " ", name, flags=re.I)
    name = re.sub(r"\b(v\d+(\.\d+)*)\b", " ", name, flags=re.I)
    name = re.sub(
        r"\b(rev|proto|beta|demo|encrypted|decrypted|bluray|brrip|webrip|web-dl|hdtv|x264|x265|hevc)\b",
        " ",
        name,
        flags=re.I,
    )
    name = re.sub(r"[_\.]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -_")
    return html.unescape(name)


def slugify(title: str) -> str:
    title = title.lower()
    title = title.replace("&", " and ")
    title = re.sub(r"[^a-z0-9]+", "-", title)
    return title.strip("-")


def title_variants(title: str) -> list[str]:
    variants = [title]
    article_match = re.match(r"(.+),\s+(the|a|an)$", title, re.I)
    if article_match:
        variants.append(f"{article_match.group(2)} {article_match.group(1)}")

    spaced_initialism = re.sub(
        r"\b((?:[A-Z]\s+){1,5}[A-Z])\b",
        lambda match: match.group(1).replace(" ", ""),
        title,
    )
    if spaced_initialism != title:
        variants.append(spaced_initialism)
        variants.append(
            re.sub(
                r"\b((?:[A-Z]\s+){1,5}[A-Z])\b",
                lambda match: ".".join(match.group(1).split()) + ".",
                title,
            )
        )

    creator_prefix = re.match(r"^[A-Z]\s+[A-Z][A-Za-z'.-]+\s+(.+)$", title)
    if creator_prefix:
        variants.append(creator_prefix.group(1))

    director_cut = re.sub(r"\s+-\s+The Director'?s Cut$", "", title, flags=re.I)
    if director_cut != title:
        variants.append(director_cut)

    seen: set[str] = set()
    deduped = []
    for variant in variants:
        normalized = re.sub(r"\s+", " ", variant).strip(" -")
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            deduped.append(normalized)
    return deduped


def slug_candidates(title: str) -> list[str]:
    seen: set[str] = set()
    candidates = []
    for variant in title_variants(title):
        slug = slugify(variant)
        if slug and slug not in seen:
            seen.add(slug)
            candidates.append(slug)
    return candidates


def normalized_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if token not in {"the", "a", "an", "of", "and"}
    }


def title_similarity(query: str, candidate: str) -> float:
    query_norm = " ".join(sorted(normalized_tokens(query)))
    candidate_norm = " ".join(sorted(normalized_tokens(candidate)))
    if not query_norm or not candidate_norm:
        return 0.0
    ratio = difflib.SequenceMatcher(None, query_norm, candidate_norm).ratio()
    query_tokens = set(query_norm.split())
    candidate_tokens = set(candidate_norm.split())
    overlap = len(query_tokens & candidate_tokens) / len(query_tokens | candidate_tokens)
    return (ratio + overlap) / 2


def strip_trailing_version_token(title: str) -> str:
    return re.sub(r"\s+(2|3|4|5|ii|iii|iv|v)$", "", title, flags=re.I).strip()


def meaningful_extra_tokens(source: set[str], target: set[str]) -> set[str]:
    version_tokens = {"2", "3", "4", "5", "ii", "iii", "iv", "v"}
    return (target - source) - version_tokens


def differs_only_by_version_token(left: set[str], right: set[str]) -> bool:
    version_tokens = {"2", "3", "4", "5", "ii", "iii", "iv", "v"}
    difference = left ^ right
    return bool(difference) and difference <= version_tokens


def normalize_context_name(name: str) -> str:
    name = name.lower()
    name = re.sub(r"\([^)]*\)", " ", name)
    name = re.sub(r"[^a-z0-9]+", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def context_names(path: Path) -> list[str]:
    parts = [path, *path.parents]
    return [normalize_context_name(part.name) for part in parts if part.name]


def platform_from_context(folder: Path) -> str | None:
    for folder_name in context_names(folder):
        for key, value in sorted(PLATFORM_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
            if re.search(rf"(^|\s){re.escape(key)}($|\s)", folder_name):
                return value
    return None


def kind_from_context(folder: Path, platform: str | None) -> str:
    for folder_name in context_names(folder):
        for key, value in sorted(MEDIA_CONTEXT_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
            if re.search(rf"(^|\s){re.escape(key)}($|\s)", folder_name):
                return value
    return "game"


def parse_metacritic_score(page: str, expected_slug: str | None = None) -> int | None:
    nuxt_score = parse_metacritic_nuxt_score(page, expected_slug)
    if nuxt_score is not None:
        return nuxt_score
    if expected_slug and "__NUXT_DATA__" in page:
        return None

    patterns = [
        r'"metascore"\s*:\s*(\d{1,3})',
        r'"criticScoreSummary"\s*:\s*\{[^{}]*"score"\s*:\s*(\d{1,3})',
        r'"score"\s*:\s*(\d{1,3})\s*,\s*"max"\s*:\s*100',
        r"metascore_w[^>]*>\s*(\d{1,3})\s*<",
    ]
    for pattern in patterns:
        match = re.search(pattern, page, re.I | re.S)
        if match:
            score = int(match.group(1))
            if 0 <= score <= 100:
                return score

    return None


def parse_metacritic_nuxt_score(page: str, expected_slug: str | None = None) -> int | None:
    match = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not match:
        return None
    try:
        data = json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None

    def resolve(value: Any) -> Any:
        if isinstance(value, int) and 0 <= value < len(data):
            return data[value]
        return value

    for item in data:
        if not isinstance(item, dict) or "criticScoreSummary" not in item or "title" not in item:
            continue
        item_type = resolve(item.get("type"))
        if isinstance(item_type, str) and not item_type.endswith("-title"):
            continue
        slug = resolve(item.get("slug"))
        if expected_slug and slug != expected_slug:
            continue
        summary = resolve(item["criticScoreSummary"])
        if not isinstance(summary, dict):
            continue
        score = resolve(summary.get("score"))
        if isinstance(score, (int, float)) and 0 <= score <= 100:
            return int(round(score))
    return None


def parse_metacritic_search_slugs(page: str, query: str, limit: int = 5) -> list[str]:
    match = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not match:
        return []
    try:
        data = json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    def resolve(value: Any) -> Any:
        if isinstance(value, int) and 0 <= value < len(data):
            return data[value]
        return value

    candidates: list[tuple[float, int, str]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict) or "title" not in item or "slug" not in item:
            continue
        item_type = resolve(item.get("type"))
        if item_type != "game-title":
            continue
        title = resolve(item["title"])
        slug = resolve(item["slug"])
        if isinstance(title, str) and isinstance(slug, str):
            candidates.append((title_similarity(query, title), index, slug))

    seen: set[str] = set()
    slugs = []
    for score, _index, slug in sorted(candidates, key=lambda item: (-item[0], item[1])):
        if score < 0.45 or slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
        if len(slugs) >= limit:
            break
    return slugs


def parse_metacritic_browse_entries(page: str) -> list[PlatformRating]:
    match = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not match:
        return []
    try:
        data = json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    def resolve(value: Any) -> Any:
        if isinstance(value, int) and 0 <= value < len(data):
            return data[value]
        return value

    entries = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict) or "title" not in item or "slug" not in item:
            continue
        item_type = resolve(item.get("type"))
        if item_type != "game-title":
            continue
        title = resolve(item.get("title"))
        slug = resolve(item.get("slug"))
        summary = resolve(item.get("criticScoreSummary"))
        score = resolve(summary.get("score")) if isinstance(summary, dict) else None
        if not isinstance(title, str) or not isinstance(slug, str):
            continue
        if not isinstance(score, (int, float)) or not 0 <= score <= 100:
            continue
        if slug in seen:
            continue
        seen.add(slug)
        entries.append(
            PlatformRating(
                title=title,
                slug=slug,
                metacritic=int(round(score)),
                metacritic_url=f"{METACRITIC_BASE_URL}/game/{slug}/",
                updated_at=time.time(),
            )
        )
    return entries


def parse_metacritic_total_pages(page: str) -> int:
    matches = re.findall(
        r'<span[^>]*class="[^"]*\bc-navigation-pagination__item-content\b[^"]*"[^>]*>\s*(\d+)\s*</span>',
        page,
        re.I,
    )
    pages = [int(match) for match in matches]
    return max(pages, default=1)


def metacritic_browse_url(platform: str, page: int) -> str:
    return f"{METACRITIC_BASE_URL}/browse/game/{platform}/all/all-time/metascore/?page={page}"


def fetch_metacritic_platform_page(platform: str, page_number: int, timeout: float) -> list[PlatformRating]:
    url = metacritic_browse_url(platform, page_number)
    log(f"  Metacritic platform page: {url}")
    return parse_metacritic_browse_entries(fetch_text(url, timeout))


def fetch_metacritic_platform_ratings(
    platform: str,
    timeout: float,
    delay: float,
    workers: int,
    max_pages: int | None = None,
) -> list[PlatformRating]:
    if platform not in METACRITIC_GAME_PLATFORMS:
        log(f"Warning: {platform!r} is not in the known Metacritic platform list; trying it anyway.")

    ratings: list[PlatformRating] = []
    seen: set[str] = set()

    first_url = metacritic_browse_url(platform, 1)
    log(f"  Metacritic platform page: {first_url}")
    first_page = fetch_text(first_url, timeout)
    total_pages = parse_metacritic_total_pages(first_page)
    if max_pages is not None:
        total_pages = min(total_pages, max_pages)

    page_results: dict[int, list[PlatformRating]] = {
        1: parse_metacritic_browse_entries(first_page) if total_pages >= 1 else []
    }
    page_numbers = list(range(2, total_pages + 1))

    worker_count = max(1, workers)
    if worker_count == 1:
        for page_number in page_numbers:
            if delay:
                time.sleep(delay)
            page_results[page_number] = fetch_metacritic_platform_page(platform, page_number, timeout)
    elif page_numbers:
        if delay:
            log("--delay is ignored for Metacritic platform catalog pages when --workers is greater than 1.")
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(fetch_metacritic_platform_page, platform, page_number, timeout): page_number
                for page_number in page_numbers
            }
            for future in concurrent.futures.as_completed(futures):
                page_results[futures[future]] = future.result()

    for page_number in sorted(page_results):
        for entry in page_results[page_number]:
            if entry.slug in seen:
                continue
            seen.add(entry.slug)
            ratings.append(entry)
    return ratings


def search_metacritic_slugs(title: str, timeout: float) -> list[str]:
    seen: set[str] = set()
    slugs = []
    for variant in title_variants(title):
        url = f"https://www.metacritic.com/search/{urllib.parse.quote(variant)}/"
        try:
            log(f"  Metacritic search: {url}")
            page = fetch_text(url, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        for slug in parse_metacritic_search_slugs(page, variant):
            if slug not in seen:
                seen.add(slug)
                slugs.append(slug)
    return slugs


def metacritic_urls(kind: str, slug: str, platform: str | None) -> list[str]:
    urls = []
    if kind == "game" and platform:
        urls.append(f"https://www.metacritic.com/game/{slug}/critic-reviews/?platform={platform}")
    urls.append(f"https://www.metacritic.com/{kind}/{slug}/")
    return urls


def get_metacritic(title: str, kind: str, platform: str | None, timeout: float) -> tuple[int | None, str | None, str]:
    saw_page = False
    saw_404 = False
    searched = False
    slugs = slug_candidates(title)
    for slug in slugs:
        for url in metacritic_urls(kind, slug, platform):
            try:
                log(f"  Metacritic: {url}")
                page = fetch_text(url, timeout)
                saw_page = True
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    saw_404 = True
                    log("  Metacritic: 404, trying next URL if available")
                    continue
                raise
            score = parse_metacritic_score(page, slug)
            if score is not None:
                return score, url, "ok"

    if kind == "game" and saw_404:
        searched = True
        for slug in search_metacritic_slugs(title, timeout):
            if slug in slugs:
                continue
            for url in metacritic_urls(kind, slug, platform):
                try:
                    log(f"  Metacritic: {url}")
                    page = fetch_text(url, timeout)
                    saw_page = True
                except urllib.error.HTTPError as exc:
                    if exc.code == 404:
                        log("  Metacritic: 404, trying next URL if available")
                        continue
                    raise
                score = parse_metacritic_score(page, slug)
                if score is not None:
                    return score, url, "ok"

    if saw_page:
        return None, None, "not found"
    return None, None, "metacritic 404" if searched or saw_404 else "not found"


def get_opencritic(title: str, timeout: float) -> tuple[int | None, str | None]:
    # OpenCritic does not cover older handheld/catalog titles as thoroughly as
    # Metacritic. This public endpoint is used by their web app and may change.
    encoded = urllib.parse.quote(title)
    search_url = f"https://api.opencritic.com/api/meta/search?criteria={encoded}"
    log(f"  OpenCritic search: {search_url}")
    data = json.loads(fetch_text(search_url, timeout))
    games = data.get("games") if isinstance(data, dict) else None
    if not games:
        return None, None

    first = games[0]
    game_id = first.get("id")
    if not game_id:
        return None, None

    detail_url = f"https://api.opencritic.com/api/game/{game_id}"
    log(f"  OpenCritic detail: {detail_url}")
    detail = json.loads(fetch_text(detail_url, timeout))
    score = detail.get("topCriticScore") or detail.get("medianScore")
    if score is None:
        return None, None
    score_int = int(round(float(score)))
    slug = first.get("url") or first.get("name") or str(game_id)
    if isinstance(slug, str) and not slug.startswith("/"):
        slug = "/" + slug.strip("/")
    public_url = f"https://opencritic.com{slug}" if isinstance(slug, str) else None
    return score_int, public_url


def load_cache_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_cache(path: Path) -> dict[str, Rating]:
    raw = load_cache_payload(path)
    return {key: Rating.from_dict(value) for key, value in raw.get("items", {}).items()}


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.{time.time_ns()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    for attempt in range(5):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.2 * (attempt + 1))


def save_cache(path: Path, cache: dict[str, Rating]) -> None:
    with CACHE_WRITE_LOCK:
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "items": {key: dataclasses.asdict(value) for key, value in sorted(cache.items())},
        }
        atomic_write_json(path, payload)


def system_cache_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "score-sort"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "score-sort"
    return base / SYSTEM_CACHE_NAME


def load_system_platform_cache(path: Path) -> dict[str, Any]:
    payload = load_cache_payload(path)
    platforms = payload.get("platforms", {})
    if isinstance(platforms, dict):
        return payload

    # Backward compatibility for the short-lived folder-cache layout.
    legacy = payload.get("metacritic_platforms", {})
    return {"version": 1, "updated_at": payload.get("updated_at"), "platforms": legacy if isinstance(legacy, dict) else {}}


def load_platform_ratings(path: Path, platform: str) -> list[PlatformRating]:
    raw = load_system_platform_cache(path)
    platform_cache = raw.get("platforms", {})
    if not isinstance(platform_cache, dict):
        return []
    platform_data = platform_cache.get(platform, {})
    if not isinstance(platform_data, dict):
        return []
    items = platform_data.get("items", [])
    if not isinstance(items, list):
        return []
    ratings = []
    for item in items:
        if isinstance(item, dict):
            try:
                ratings.append(PlatformRating.from_dict(item))
            except (KeyError, TypeError, ValueError):
                continue
    return ratings


def platform_cache_created_at(path: Path, platform: str) -> float | None:
    raw = load_system_platform_cache(path)
    platforms = raw.get("platforms", {})
    if not isinstance(platforms, dict):
        return None
    platform_data = platforms.get(platform, {})
    if not isinstance(platform_data, dict):
        return None
    created_at = platform_data.get("created_at") or platform_data.get("updated_at")
    return float(created_at) if isinstance(created_at, (int, float)) else None


def is_platform_cache_fresh(path: Path, platform: str, max_age_days: int | None) -> bool:
    if max_age_days is None:
        return True
    created_at = platform_cache_created_at(path, platform)
    if created_at is None:
        return False
    return time.time() - created_at <= max_age_days * 24 * 60 * 60


def preserve_platform_user_scores(
    existing: list[PlatformRating],
    refreshed: list[PlatformRating],
    created_at: float,
) -> list[PlatformRating]:
    existing_by_slug = {rating.slug: rating for rating in existing}
    existing_by_url = {rating.metacritic_url: rating for rating in existing}
    merged = []
    for rating in refreshed:
        previous = existing_by_slug.get(rating.slug) or existing_by_url.get(rating.metacritic_url)
        rating.updated_at = created_at
        if previous is not None:
            rating.user_score = previous.user_score
            rating.user_score_url = previous.user_score_url
            rating.user_score_updated_at = previous.user_score_updated_at
        merged.append(rating)
    return merged


def save_platform_ratings(path: Path, platform: str, ratings: list[PlatformRating], created_at: float | None = None) -> None:
    with CACHE_WRITE_LOCK:
        payload = load_system_platform_cache(path)
        platform_cache = payload.get("platforms", {})
        if not isinstance(platform_cache, dict):
            platform_cache = {}
        created = created_at or time.time()
        platform_cache[platform] = {
            "platform": platform,
            "created_at": created,
            "updated_at": created,
            "items": [dataclasses.asdict(rating) for rating in ratings],
        }
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "platforms": platform_cache,
        }
        atomic_write_json(path, payload)


def iter_games(folder: Path, extensions: set[str], include_dirs: bool) -> list[Path]:
    entries: list[Path] = []
    for child in folder.iterdir():
        if child.name == CACHE_NAME or child.name.startswith("."):
            continue
        if child.is_file() and child.suffix.lower() in extensions:
            entries.append(child)
        elif include_dirs and child.is_dir():
            entries.append(child)
    return sorted(entries, key=lambda p: p.name.lower())


def best_platform_rating_match(title: str, ratings: list[PlatformRating]) -> PlatformRating | None:
    if not ratings:
        return None

    by_slug = {rating.slug: rating for rating in ratings}
    for slug in slug_candidates(title):
        match = by_slug.get(slug)
        if match is not None:
            return match

    normalized_title = normalize_context_name(title)
    for rating in ratings:
        if normalize_context_name(rating.title) == normalized_title:
            return rating

    best_score = 0.0
    best_match: PlatformRating | None = None
    title_tokens = normalized_tokens(title)
    title_stripped = normalize_context_name(strip_trailing_version_token(title))
    for rating in ratings:
        score = title_similarity(title, rating.title)
        rating_tokens = normalized_tokens(rating.title)
        if title_stripped and title_stripped == normalize_context_name(strip_trailing_version_token(rating.title)):
            if differs_only_by_version_token(title_tokens, rating_tokens):
                continue
        if title_tokens and title_tokens <= rating_tokens and meaningful_extra_tokens(title_tokens, rating_tokens):
            score = max(score, 0.90)
        elif rating_tokens and rating_tokens <= title_tokens and meaningful_extra_tokens(rating_tokens, title_tokens):
            score = max(score, 0.88)

        rating_stripped = normalize_context_name(strip_trailing_version_token(rating.title))
        if title_stripped and title_stripped == rating_stripped:
            score = max(score, 0.80)

        if score > best_score:
            best_score = score
            best_match = rating
    return best_match if best_score >= 0.82 else None


def ensure_platform_ratings(
    cache_path: Path,
    platform: str | None,
    refresh: bool,
    timeout: float,
    delay: float,
    workers: int,
    max_pages: int | None,
    max_age_days: int | None,
) -> list[PlatformRating]:
    if not platform:
        return []
    cached = [] if refresh or not is_platform_cache_fresh(cache_path, platform, max_age_days) else load_platform_ratings(
        cache_path,
        platform,
    )
    if cached:
        log(f"Using cached Metacritic platform catalog for {platform}: {len(cached)} ratings.")
        return cached
    log(f"Fetching Metacritic platform catalog for {platform}...")
    previous = load_platform_ratings(cache_path, platform)
    ratings = fetch_metacritic_platform_ratings(platform, timeout, delay, workers, max_pages)
    created_at = time.time()
    ratings = preserve_platform_user_scores(previous, ratings, created_at)
    save_platform_ratings(cache_path, platform, ratings, created_at)
    log(f"Cached Metacritic platform catalog for {platform}: {len(ratings)} ratings.")
    return ratings


def lookup_rating(
    index: int,
    total: int,
    entry: Path,
    kind: str,
    platform: str | None,
    sources: set[str],
    timeout: float,
    platform_ratings: list[PlatformRating] | None = None,
    allow_metacritic_fallback: bool = True,
) -> Rating:
    title = clean_title(entry)
    rating = Rating(title=title, path=str(entry.resolve()), kind=kind, platform=platform)
    context = f"{kind}" + (f"/{platform}" if platform else "")
    log(f"[{index}/{total}] Looking up {title} ({context})...")
    status_parts = []
    if "metacritic" in sources:
        using_platform_catalog = kind == "game" and platform_ratings is not None
        platform_match = best_platform_rating_match(title, platform_ratings) if using_platform_catalog else None
        if platform_match is not None:
            rating.metacritic = platform_match.metacritic
            rating.metacritic_url = platform_match.metacritic_url
            log(f"  Metacritic platform match: {platform_match.title} ({platform_match.metacritic})")
        elif using_platform_catalog and not allow_metacritic_fallback:
            status_parts.append("not found")
        else:
            try:
                rating.metacritic, rating.metacritic_url, metacritic_status = get_metacritic(
                    title,
                    kind,
                    platform,
                    timeout,
                )
                if metacritic_status != "ok":
                    status_parts.append(metacritic_status)
            except Exception as exc:  # noqa: BLE001 - preserve lookup failure in cache.
                status_parts.append(f"metacritic error: {exc}")

    if kind == "game" and "opencritic" in sources:
        try:
            rating.opencritic, rating.opencritic_url = get_opencritic(title, timeout)
        except Exception as exc:  # noqa: BLE001 - preserve lookup failure in cache.
            status_parts.append(f"opencritic error: {exc}")
    elif kind != "game" and "opencritic" in sources:
        status_parts.append("opencritic skipped for non-game entry")

    rating.calculate()
    if status_parts:
        rating.status = "; ".join(status_parts)
    if rating.combined is None and not status_parts:
        rating.status = "not found"
    log(
        f"[{index}/{total}] Result: "
        f"MC={rating.metacritic if rating.metacritic is not None else '-'} "
        f"OC={rating.opencritic if rating.opencritic is not None else '-'} "
        f"combined={rating.combined if rating.combined is not None else '-'} "
        f"status={rating.status}",
    )
    return rating


def refresh_ratings(
    folder: Path,
    entries: list[Path],
    cache: dict[str, Rating],
    kind: str,
    platform: str | None,
    sources: set[str],
    refresh: bool,
    refresh_failed: bool,
    delay: float,
    timeout: float,
    workers: int,
    platform_ratings: list[PlatformRating] | None = None,
    allow_metacritic_fallback: bool = True,
) -> None:
    pending: list[tuple[int, Path]] = []
    for index, entry in enumerate(entries, start=1):
        key = str(entry.resolve())
        cached = cache.get(key)
        context_changed = cached is not None and (cached.kind != kind or cached.platform != platform)
        retryable_failure = cached is not None and is_retryable_cached_failure(cached)
        refreshable_failure = cached is not None and refresh_failed and is_failed_cached_entry(cached)
        if (
            cached is not None
            and not refresh
            and not context_changed
            and not retryable_failure
            and not refreshable_failure
        ):
            continue
        pending.append((index, entry))

    if not pending:
        return

    worker_count = max(1, workers)
    if worker_count == 1:
        for index, entry in pending:
            rating = lookup_rating(
                index,
                len(entries),
                entry,
                kind,
                platform,
                sources,
                timeout,
                platform_ratings,
                allow_metacritic_fallback,
            )
            cache[rating.path] = rating
            save_cache(folder / CACHE_NAME, cache)
            if delay:
                time.sleep(delay)
        return

    if delay:
        log("--delay is ignored when --workers is greater than 1.")
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(
                lookup_rating,
                index,
                len(entries),
                entry,
                kind,
                platform,
                sources,
                timeout,
                platform_ratings,
                allow_metacritic_fallback,
            )
            for index, entry in pending
        ]
        for future in concurrent.futures.as_completed(futures):
            rating = future.result()
            cache[rating.path] = rating
    save_cache(folder / CACHE_NAME, cache)


def sorted_ratings(cache: dict[str, Rating], entries: list[Path]) -> list[Rating]:
    keys = {str(entry.resolve()) for entry in entries}
    rows = [rating for key, rating in cache.items() if key in keys and is_rankable(rating)]
    return sorted(
        rows,
        key=lambda row: (
            row.combined is None,
            -(row.combined or -1),
            row.title.lower(),
        ),
    )


def is_rankable(rating: Rating) -> bool:
    if rating.combined is not None:
        return True
    return rating.status == "not found"


def is_retryable_cached_failure(rating: Rating) -> bool:
    return "timed out" in rating.status.lower()


def is_failed_cached_entry(rating: Rating) -> bool:
    return rating.status != "ok"


def order_rows(rows: list[Rating], order: str) -> list[Rating]:
    if order == "best":
        return rows
    if order == "worst":
        rated = list(reversed([row for row in rows if row.combined is not None]))
        unrated = [row for row in rows if row.combined is None]
        return rated + unrated
    raise ValueError(f"Unknown order: {order}")


def parse_score_range(expression: str) -> tuple[float | None, float | None, bool, bool]:
    value = expression.strip()
    if not value:
        raise ValueError("--range cannot be empty")

    comparison = re.fullmatch(r"(<=|>=|<|>)\s*(\d+(?:\.\d+)?)", value)
    if comparison:
        operator, raw_score = comparison.groups()
        score = float(raw_score)
        validate_score_bound(score)
        if operator == ">":
            return score, None, False, True
        if operator == ">=":
            return score, None, True, True
        if operator == "<":
            return None, score, True, False
        return None, score, True, True

    span = re.fullmatch(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", value)
    if span:
        low = float(span.group(1))
        high = float(span.group(2))
        validate_score_bound(low)
        validate_score_bound(high)
        if low > high:
            raise ValueError("--range lower bound must be less than or equal to upper bound")
        return low, high, True, True

    raise ValueError("--range must look like 1-50, >70, >=70, <40, or <=40")


def validate_score_bound(score: float) -> None:
    if not 0 <= score <= 100:
        raise ValueError("--range scores must be between 0 and 100")


def score_in_range(score: float, parsed_range: tuple[float | None, float | None, bool, bool]) -> bool:
    low, high, include_low, include_high = parsed_range
    if low is not None and (score < low or (score == low and not include_low)):
        return False
    if high is not None and (score > high or (score == high and not include_high)):
        return False
    return True


def filter_rows_by_score_range(rows: list[Rating], expression: str | None) -> list[Rating]:
    if expression is None:
        return rows
    parsed_range = parse_score_range(expression)
    return [row for row in rows if row.combined is not None and score_in_range(row.combined, parsed_range)]


def print_rankings(rows: list[Rating], limit: int | None = None, output_format: str = "table") -> None:
    shown = rows[:limit] if limit else rows
    if output_format == "paths":
        for row in shown:
            print(row.path)
        return
    if output_format == "json":
        print(json.dumps([dataclasses.asdict(row) for row in shown], indent=2, sort_keys=True))
        return
    if output_format == "tsv":
        print("rank\tscore\tmetacritic\topencritic\tkind\ttitle\tpath")
        for idx, row in enumerate(shown, start=1):
            score = f"{row.combined:.1f}" if row.combined is not None else ""
            mc = str(row.metacritic) if row.metacritic is not None else ""
            oc = str(row.opencritic) if row.opencritic is not None else ""
            print(f"{idx}\t{score}\t{mc}\t{oc}\t{row.kind}\t{row.title}\t{row.path}")
        return

    print("\nRank  Score  MC   OC   Type   Title")
    print("----  -----  ---  ---  -----  -----")
    for idx, row in enumerate(shown, start=1):
        score = f"{row.combined:.1f}" if row.combined is not None else "N/A"
        mc = str(row.metacritic) if row.metacritic is not None else "-"
        oc = str(row.opencritic) if row.opencritic is not None else "-"
        print(f"{idx:>4}  {score:>5}  {mc:>3}  {oc:>3}  {row.kind:<5}  {row.title}")


def bottom_rows(rows: list[Rating], count: int) -> list[Rating]:
    rated = [row for row in rows if row.combined is not None]
    return list(reversed(rated[-count:]))


def delete_rows(rows: list[Rating], yes: bool) -> None:
    for row in rows:
        path = Path(row.path)
        print(f"{'Deleting' if yes else 'Would delete'}: {path}")
        if yes:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()


def move_rows(rows: list[Rating], destination: Path, yes: bool) -> None:
    if yes:
        destination.mkdir(parents=True, exist_ok=True)
    for row in rows:
        source = Path(row.path)
        target = destination / source.name
        print(f"{'Moving' if yes else 'Would move'}: {source} -> {target}")
        if yes:
            if target.exists():
                raise FileExistsError(f"Destination already exists: {target}")
            shutil.move(str(source), str(target))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rank games, movies, or TV folders using Metacritic/OpenCritic, with a local JSON cache.",
    )
    parser.add_argument("folder", type=Path, help="Folder containing files or child folders to rank.")
    parser.add_argument(
        "--action",
        choices=("rank", "delete-bottom", "move-top"),
        default="rank",
        help="What to do after ratings are cached.",
    )
    parser.add_argument("--count", type=int, default=20, help="Number of entries for delete-bottom or move-top.")
    parser.add_argument("--dest", type=Path, help="Destination folder for move-top.")
    parser.add_argument("--yes", action="store_true", help="Actually delete or move files. Without this, dry-run only.")
    parser.add_argument("--refresh", action="store_true", help="Ignore cached entries and look them up again.")
    parser.add_argument(
        "--refresh-failed",
        action="store_true",
        help="Ignore cached failed entries only, such as not found, 404, and lookup errors.",
    )
    parser.add_argument("--limit", type=int, help="Limit rows shown for rank output.")
    parser.add_argument(
        "--order",
        choices=("best", "worst"),
        default="best",
        help="Sort rank output best-first or worst-first. Use --order worst for pipe-friendly tail cleanup.",
    )
    parser.add_argument(
        "--format",
        choices=("table", "paths", "json", "tsv"),
        default="table",
        help="Output format for rank action. Use paths for shell pipelines.",
    )
    parser.add_argument(
        "--range",
        dest="score_range",
        help="Only include scored entries in a range, such as 1-50, >70, >=70, <40, or <=40.",
    )
    parser.add_argument("--max-entries", type=int, help="Only scan the first N entries, useful for test runs.")
    parser.add_argument("--include-dirs", action="store_true", help="Treat first-level child folders as entries too.")
    parser.add_argument(
        "--kind",
        choices=("game", "movie", "tv"),
        help="Override context detection. Defaults to inferred from this folder or its parents.",
    )
    parser.add_argument("--platform", help="Metacritic platform slug, such as nintendo-ds, 3ds, ps2, or xbox-360.")
    parser.add_argument(
        "--extensions",
        default=DEFAULT_EXTENSIONS,
        help="Comma-separated file extensions to scan.",
    )
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds to wait between uncached lookups.")
    parser.add_argument("--timeout", type=float, default=8.0, help="Seconds before an individual HTTP request times out.")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of parallel lookup workers. Defaults to 1; try 4-8 for faster refreshes.",
    )
    parser.add_argument(
        "--sources",
        choices=("metacritic", "opencritic", "both"),
        default="metacritic",
        help="Rating sources to query. Defaults to Metacritic only; OpenCritic is useful mostly for modern games.",
    )
    parser.add_argument(
        "--no-platform-catalog",
        action="store_true",
        help="Skip the Metacritic all-time platform catalog and fall back to per-title lookups.",
    )
    parser.add_argument(
        "--lookup-missing",
        action="store_true",
        help="After a platform catalog miss, fall back to Metacritic per-title page/search lookups.",
    )
    parser.add_argument(
        "--catalog-max-pages",
        type=int,
        help="Limit Metacritic platform catalog pages fetched, useful for parser smoke tests.",
    )
    parser.add_argument(
        "--platform-cache",
        type=Path,
        help="Override the system-wide Metacritic platform cache path.",
    )
    parser.add_argument(
        "--platform-cache-days",
        type=int,
        default=DEFAULT_PLATFORM_CACHE_DAYS,
        help="Refresh a platform catalog after this many days. Use 0 with --refresh for a forced rebuild.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.workers < 1:
        print("--workers must be at least 1.", file=sys.stderr)
        return 2
    if args.score_range is not None:
        try:
            parse_score_range(args.score_range)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        print(f"Folder does not exist: {folder}", file=sys.stderr)
        return 2

    extensions = {
        ext.strip().lower() if ext.strip().startswith(".") else f".{ext.strip().lower()}"
        for ext in args.extensions.split(",")
        if ext.strip()
    }
    entries = iter_games(folder, extensions, args.include_dirs)
    if args.max_entries is not None:
        entries = entries[: args.max_entries]
    if not entries:
        print("No matching entries found.")
        return 1

    platform = args.platform or platform_from_context(folder)
    kind = args.kind or kind_from_context(folder, platform)
    sources = {"metacritic", "opencritic"} if args.sources == "both" else {args.sources}
    log(
        f"Detected context: kind={kind}" + (f", platform={platform}" if platform else "")
        + f", sources={','.join(sorted(sources))}",
    )
    cache_path = folder / CACHE_NAME
    platform_cache_path = args.platform_cache.expanduser().resolve() if args.platform_cache else system_cache_path()
    cache = load_cache(cache_path)
    platform_ratings = None
    if kind == "game" and platform and "metacritic" in sources and not args.no_platform_catalog:
        log(f"Metacritic platform cache: {platform_cache_path}")
        platform_ratings = ensure_platform_ratings(
            platform_cache_path,
            platform,
            args.refresh,
            args.timeout,
            args.delay,
            args.workers,
            args.catalog_max_pages,
            args.platform_cache_days,
        )
    refresh_ratings(
        folder,
        entries,
        cache,
        kind,
        platform,
        sources,
        args.refresh,
        args.refresh_failed,
        args.delay,
        args.timeout,
        args.workers,
        platform_ratings,
        args.lookup_missing or platform_ratings is None,
    )
    rows = sorted_ratings(cache, entries)
    action_rows = filter_rows_by_score_range(rows, args.score_range)
    ordered_rows = filter_rows_by_score_range(order_rows(rows, args.order), args.score_range)
    print_rankings(ordered_rows, args.limit, args.format)

    if args.action == "delete-bottom":
        selected = bottom_rows(action_rows, args.count)
        print(f"\nSelected bottom {len(selected)} rated entries.")
        delete_rows(selected, args.yes)
    elif args.action == "move-top":
        if not args.dest:
            print("--dest is required for move-top.", file=sys.stderr)
            return 2
        selected = [row for row in action_rows if row.combined is not None][: args.count]
        print(f"\nSelected top {len(selected)} rated entries.")
        move_rows(selected, args.dest.expanduser().resolve(), args.yes)

    if args.action != "rank" and not args.yes:
        print("\nDry-run only. Add --yes to perform the action.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
