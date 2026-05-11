#!/usr/bin/env python3
"""
Rank games, movies, or TV folders by Metacritic/OpenCritic scores.

The script stores platform catalogs and filename matches in a system-wide cache.
Destructive operations are dry-run unless --yes is provided.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import csv
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

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)


LEGACY_CACHE_NAME = ".rom_rating_cache.json"
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
COMPOUND_TITLE_SUFFIXES = (
    ".nkit.iso",
    ".nkit.gcz",
    ".nkit.rvz",
    ".xiso.iso",
    ".ciso.iso",
)
LEGACY_FILENAME_KEY_FORMAT_SUFFIXES = ("-nkit", "-xiso", "-ciso")
KNOWN_TITLE_SUFFIXES = tuple(
    sorted(
        {
            *COMPOUND_TITLE_SUFFIXES,
            *(ext.strip().lower() for ext in DEFAULT_EXTENSIONS.split(",") if ext.strip()),
        },
        key=len,
        reverse=True,
    )
)
CACHE_WRITE_LOCK = threading.Lock()
DEFAULT_PLATFORM_CACHE_DAYS = 30
VERBOSE = False
DEBUG = False
UNCACHED_LOOKUP_ATTR = "_uncached_lookup_performed"
DEBUG_TIMINGS_ATTR = "_debug_timings"
MATCH_REVIEW_CANDIDATES_ATTR = "_match_review_candidates"
AUTO_MATCH_THRESHOLD = 0.93
REVIEW_MATCH_THRESHOLD = 0.80
MAX_REVIEW_CANDIDATES = 3
FILENAME_MATCH_LOOKUP_VERSION = 6
METACRITIC_SEARCH_SLUG_LIMIT = 10
MIN_METACRITIC_USER_RATINGS_FOR_COMBINED = 4
METACRITIC_PRODUCT_PAGE_ATTEMPT_STATUSES = {"ok", "not found"}
METACRITIC_CACHEABLE_MISS_STATUSES = {"not found"}
PROGRESS_CONSOLE = Console(stderr=True)
DEBUG_WRITE_LOCK = threading.Lock()


def log(message: str) -> None:
    if VERBOSE:
        print(message, file=sys.stderr, flush=True)


def warn(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def debug_log(message: str) -> None:
    if not DEBUG:
        return
    with DEBUG_WRITE_LOCK:
        PROGRESS_CONSOLE.print(message, highlight=False, markup=False)


def mark_uncached_lookup(rating: "Rating") -> None:
    setattr(rating, UNCACHED_LOOKUP_ATTR, True)


def used_uncached_lookup(rating: "Rating") -> bool:
    return bool(getattr(rating, UNCACHED_LOOKUP_ATTR, False))


def add_debug_timing(target: Any, label: str, elapsed: float) -> None:
    if not DEBUG:
        return
    timings = getattr(target, DEBUG_TIMINGS_ATTR, None)
    if timings is None:
        timings = []
        setattr(target, DEBUG_TIMINGS_ATTR, timings)
    timings.append((label, elapsed))


def copy_debug_timings(target: Any, timings: list[tuple[str, float]]) -> None:
    if not DEBUG:
        return
    for label, elapsed in timings:
        add_debug_timing(target, label, elapsed)


@contextlib.contextmanager
def debug_timing(target: Any, label: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        add_debug_timing(target, label, time.perf_counter() - start)


def format_seconds(seconds: float) -> str:
    if seconds >= 10:
        return f"{seconds:.1f}s"
    return f"{seconds:.3f}s"


def format_debug_timing_summary(target: Any) -> str:
    timings = getattr(target, DEBUG_TIMINGS_ATTR, []) or []
    if not timings:
        return "no timings"
    totals: dict[str, float] = {}
    order = []
    for label, elapsed in timings:
        if label not in totals:
            order.append(label)
            totals[label] = 0.0
        totals[label] += elapsed
    return ", ".join(f"{label}={format_seconds(totals[label])}" for label in order)


def debug_fetch_text(url: str, timeout: float, timings: list[tuple[str, float]], label: str) -> str:
    start = time.perf_counter()
    try:
        return fetch_text(url, timeout)
    finally:
        if DEBUG:
            timings.append((label, time.perf_counter() - start))


def add_match_review_candidate(rating: "Rating", candidate: "PlatformMatchCandidate") -> None:
    review_candidates = getattr(rating, MATCH_REVIEW_CANDIDATES_ATTR, None)
    if review_candidates is None:
        review_candidates = []
        setattr(rating, MATCH_REVIEW_CANDIDATES_ATTR, review_candidates)
    review_candidates.append(
        MatchReviewCandidate(
            local_title=rating.title,
            candidate_title=candidate.rating.title,
            candidate_slug=candidate.rating.slug,
            score=round(candidate.score, 4),
            reason=candidate.reason,
        )
    )


def rating_match_review_candidates(rating: "Rating") -> list["MatchReviewCandidate"]:
    return list(getattr(rating, MATCH_REVIEW_CANDIDATES_ATTR, []) or [])


def should_show_progress() -> bool:
    return sys.stderr.isatty()


def create_progress() -> Progress | None:
    if not should_show_progress():
        return None
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        TextColumn("{task.fields[detail]}"),
        console=PROGRESS_CONSOLE,
        transient=True,
        refresh_per_second=4,
    )


def progress_detail(value: str | Path, max_length: int = 48) -> str:
    text = str(value)
    if len(text) <= max_length:
        return text
    return f"...{text[-(max_length - 3):]}"


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

METACRITIC_PLATFORM_LABEL_ALIASES = {
    "n64": "nintendo-64",
    "nsw": "nintendo-switch",
    "switch 2": "nintendo-switch-2",
    "ps": "ps1",
    "x360": "xbox-360",
    "xbox series x s": "xbox-series-x",
    "series x": "xbox-series-x",
    "series x s": "xbox-series-x",
}

METACRITIC_REVIEW_PLATFORM_PARAMS = {
    "nintendo-ds": "ds",
    "nintendo-switch": "switch",
    "nintendo-switch-2": "switch-2",
    "game-boy-advance": "gba",
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
    metacritic_review_count: int | None = None
    metacritic_user: float | None = None
    metacritic_user_url: str | None = None
    metacritic_user_count: int | None = None
    metacritic_product_page_parsed: bool = False
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
        if scores:
            self.combined = round(sum(scores) / len(scores), 2)
        elif (
            self.metacritic_user is not None
            and self.metacritic_user_count is not None
            and self.metacritic_user_count >= MIN_METACRITIC_USER_RATINGS_FOR_COMBINED
        ):
            self.combined = round(self.metacritic_user * 10, 2)
        else:
            self.combined = None


@dataclasses.dataclass
class PlatformRating:
    title: str
    slug: str
    metacritic: int
    metacritic_url: str
    updated_at: float | None = None
    review_count: int | None = None
    user_score: float | None = None
    user_score_url: str | None = None
    user_rating_count: int | None = None
    user_score_updated_at: float | None = None
    product_page_parsed: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlatformRating":
        metacritic = int(data["metacritic"])
        review_count = int(data["review_count"]) if data.get("review_count") is not None else None
        metacritic, review_count = sanitize_metacritic_score(metacritic, review_count)
        if metacritic is None:
            raise ValueError("Metacritic score has zero critic reviews")
        user_score = float(data["user_score"]) if data.get("user_score") is not None else None
        user_rating_count = int(data["user_rating_count"]) if data.get("user_rating_count") is not None else None
        if is_suspicious_countless_user_score(user_score, user_rating_count, metacritic):
            user_score = None
            user_rating_count = None
        return cls(
            title=str(data["title"]),
            slug=str(data["slug"]),
            metacritic=metacritic,
            metacritic_url=str(data["metacritic_url"]),
            updated_at=float(data["updated_at"]) if data.get("updated_at") is not None else None,
            review_count=review_count,
            user_score=user_score,
            user_score_url=str(data["user_score_url"]) if user_score is not None and data.get("user_score_url") is not None else None,
            user_rating_count=user_rating_count,
            user_score_updated_at=(
                float(data["user_score_updated_at"])
                if user_score is not None and data.get("user_score_updated_at") is not None
                else None
            ),
            product_page_parsed=bool(data.get("product_page_parsed", False)),
        )


@dataclasses.dataclass
class FilenameMatch:
    title: str
    slug: str | None = None
    status: str = "ok"
    match_type: str = "unknown"
    metacritic: int | None = None
    metacritic_url: str | None = None
    review_count: int | None = None
    user_score: float | None = None
    user_score_url: str | None = None
    user_rating_count: int | None = None
    product_page_parsed: bool = False
    rejected_slugs: list[str] = dataclasses.field(default_factory=list)
    lookup_version: int = FILENAME_MATCH_LOOKUP_VERSION
    updated_at: float = dataclasses.field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FilenameMatch":
        metacritic = int(data["metacritic"]) if data.get("metacritic") is not None else None
        review_count = int(data["review_count"]) if data.get("review_count") is not None else None
        metacritic, review_count = sanitize_metacritic_score(metacritic, review_count)
        user_score = float(data["user_score"]) if data.get("user_score") is not None else None
        user_rating_count = int(data["user_rating_count"]) if data.get("user_rating_count") is not None else None
        if is_suspicious_countless_user_score(user_score, user_rating_count, metacritic):
            user_score = None
            user_rating_count = None
        return cls(
            title=str(data.get("title", "")),
            slug=str(data["slug"]) if data.get("slug") is not None else None,
            status=str(data.get("status", "ok")),
            match_type=str(data.get("match_type", "unknown")),
            metacritic=metacritic,
            metacritic_url=str(data["metacritic_url"]) if metacritic is not None and data.get("metacritic_url") is not None else None,
            review_count=review_count,
            user_score=user_score,
            user_score_url=str(data["user_score_url"]) if user_score is not None and data.get("user_score_url") is not None else None,
            user_rating_count=user_rating_count,
            product_page_parsed=bool(data.get("product_page_parsed", False)),
            rejected_slugs=(
                [
                    str(slug)
                    for slug in data.get("rejected_slugs", [])
                    if isinstance(slug, str) and slug
                ]
                if isinstance(data.get("rejected_slugs", []), list)
                else []
            ),
            lookup_version=int(data["lookup_version"]) if data.get("lookup_version") is not None else 0,
            updated_at=float(data["updated_at"]) if data.get("updated_at") is not None else time.time(),
        )


@dataclasses.dataclass(frozen=True)
class PlatformMatchCandidate:
    rating: PlatformRating
    score: float
    reason: str
    version_conflict: bool = False


@dataclasses.dataclass(frozen=True)
class MatchReviewCandidate:
    local_title: str
    candidate_title: str
    candidate_slug: str
    score: float
    reason: str


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


def strip_known_title_suffixes(name: str) -> str:
    stripped = name
    while True:
        lower_name = stripped.lower()
        suffix = next(
            (
                suffix
                for suffix in KNOWN_TITLE_SUFFIXES
                if lower_name.endswith(suffix) and len(stripped) > len(suffix)
            ),
            None,
        )
        if suffix is None:
            return stripped
        stripped = stripped[: -len(suffix)]


def clean_title(path: Path) -> str:
    name = strip_known_title_suffixes(path.name)
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
    title = re.sub(r"['’`]", "", title)
    title = title.replace("&", " and ")
    title = re.sub(r"[^a-z0-9]+", "-", title)
    return title.strip("-")


def title_variants(title: str) -> list[str]:
    variants = [title]
    article_match = re.match(r"(.+),\s+(the|a|an)$", title, re.I)
    if article_match:
        variants.append(f"{article_match.group(2)} {article_match.group(1)}")
    article_with_subtitle_match = re.match(r"(.+),\s+(the|a|an)(\s*[-:]\s+.+)$", title, re.I)
    if article_with_subtitle_match:
        variants.append(
            f"{article_with_subtitle_match.group(2)} "
            f"{article_with_subtitle_match.group(1)}"
            f"{article_with_subtitle_match.group(3)}"
        )

    year_suffix = re.sub(r"\s+\(\d{4}\)$", "", title)
    if year_suffix != title:
        variants.append(year_suffix)

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

    video_game_suffix = re.sub(r"\s+-\s+The Video Game$", "", title, flags=re.I)
    if video_game_suffix != title:
        variants.append(video_game_suffix)

    featuring_suffix = re.sub(
        r"\s+Featuring\s+(?:[A-Z0-9&]{2,}(?:\s+[A-Z0-9&]{2,}){0,2})$",
        "",
        title,
    )
    if featuring_suffix != title:
        variants.append(featuring_suffix)

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
        slug_variants = [variant]
        if "&" in variant:
            slug_variants.append(variant.replace("&", " "))
        for slug_variant in slug_variants:
            slug = slugify(slug_variant)
            if slug and slug not in seen:
                seen.add(slug)
                candidates.append(slug)
    return candidates


MATCH_STOPWORDS = {"the", "a", "an", "of", "and"}
VERSION_TOKEN_PATTERN = re.compile(r"^(?:\d+|i|ii|iii|iv|v|vi|vii|viii|ix|x)$", re.I)


def normalize_match_text(value: str) -> str:
    value = html.unescape(value).lower()
    value = value.replace("&", " and ")
    value = re.sub(r"['’`]", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    tokens = [token for token in value.split() if token not in MATCH_STOPWORDS]
    return " ".join(tokens)


def match_tokens(value: str) -> list[str]:
    normalized = normalize_match_text(value)
    return normalized.split() if normalized else []


def normalized_tokens(value: str) -> set[str]:
    return set(match_tokens(value))


def title_match_keys(title: str) -> set[str]:
    return {normalize_match_text(variant) for variant in title_variants(title) if normalize_match_text(variant)}


def version_tokens(value: str) -> list[str]:
    return [token for token in match_tokens(value) if VERSION_TOKEN_PATTERN.fullmatch(token)]


def has_version_conflict(left: str, right: str) -> bool:
    return version_tokens(left) != version_tokens(right)


def title_similarity(query: str, candidate: str) -> float:
    query_tokens = normalized_tokens(query)
    candidate_tokens = normalized_tokens(candidate)
    if not query_tokens or not candidate_tokens:
        return 0.0
    query_sorted = " ".join(sorted(query_tokens))
    candidate_sorted = " ".join(sorted(candidate_tokens))
    token_ratio = difflib.SequenceMatcher(None, query_sorted, candidate_sorted).ratio()
    ordered_ratio = difflib.SequenceMatcher(None, normalize_match_text(query), normalize_match_text(candidate)).ratio()
    overlap = len(query_tokens & candidate_tokens) / len(query_tokens | candidate_tokens)
    return (token_ratio + ordered_ratio + overlap) / 3


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


def metacritic_platform_slug_from_label(label: str) -> str | None:
    normalized = normalize_context_name(label)
    if not normalized:
        return None
    if normalized in PLATFORM_ALIASES:
        return PLATFORM_ALIASES[normalized]
    if normalized in METACRITIC_PLATFORM_LABEL_ALIASES:
        return METACRITIC_PLATFORM_LABEL_ALIASES[normalized]
    for platform in METACRITIC_GAME_PLATFORMS:
        if normalized == normalize_context_name(platform):
            return platform
    return None


def parse_metacritic_page_platforms(page: str) -> set[str]:
    platforms: set[str] = set()
    matches = re.finditer(
        r'<(?P<tag>[a-z][a-z0-9]*)\b'
        r'(?=[^>]*\bclass=["\'][^"\']*\bgame-platform-logo__text\b[^"\']*["\'])'
        r'[^>]*>(?P<body>.*?)</(?P=tag)>',
        page,
        re.I | re.S,
    )
    for match in matches:
        slug = metacritic_platform_slug_from_label(clean_html_text(match.group("body")))
        if slug is not None:
            platforms.add(slug)
    heading_matches = re.finditer(
        r"<h1\b[^>]*>(?P<body>.*?)</h1>",
        page,
        re.I | re.S,
    )
    for match in heading_matches:
        text = clean_html_text(match.group("body")).strip()
        platform_match = re.fullmatch(r"(.+?)\s+(?:Critic|User)\s+Reviews?", text, re.I)
        if platform_match:
            slug = metacritic_platform_slug_from_label(platform_match.group(1))
            if slug is not None:
                platforms.add(slug)
    return platforms


def metacritic_page_matches_platform(page: str, platform: str | None) -> bool:
    if not platform:
        return True
    expected_platform = metacritic_platform_slug_from_label(platform) or platform
    return expected_platform in parse_metacritic_page_platforms(page)


def metacritic_product_page_matches_single_platform(page: str, platform: str | None) -> bool:
    if not platform:
        return True
    expected_platform = metacritic_platform_slug_from_label(platform) or platform
    return parse_metacritic_page_platforms(page) == {expected_platform}


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


def normalize_metacritic_user_score(value: Any) -> float | None:
    if isinstance(value, str):
        value = value.strip().lower()
        if value == "tbd":
            return None
        if not re.fullmatch(r"\d+(?:\.\d+)?", value):
            return None
        value = float(value)
    if isinstance(value, (int, float)):
        score = float(value)
        if 0 <= score <= 10:
            return round(score, 1)
        if 10 < score <= 100:
            return round(score / 10, 1)
    return None


def is_suspicious_countless_user_score(
    user_score: float | None,
    user_rating_count: int | None,
    metacritic: int | None = None,
) -> bool:
    if user_score is None:
        return False
    if user_rating_count is None or user_rating_count == 0:
        return True
    return False


def sanitize_user_score(
    user_score: float | None,
    user_rating_count: int | None,
    metacritic: int | None = None,
) -> tuple[float | None, int | None]:
    if is_suspicious_countless_user_score(user_score, user_rating_count, metacritic):
        return None, None
    return user_score, user_rating_count


def sanitize_metacritic_score(
    score: int | None,
    review_count: int | None,
) -> tuple[int | None, int | None]:
    if review_count == 0:
        return None, None
    return score, review_count


def metacritic_critic_reviews_unavailable(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:critic reviews are not available yet|there are no critic reviews for this \w+ yet)\b",
            text,
            re.I,
        )
    )


def normalize_count(value: Any) -> int | None:
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not re.fullmatch(r"\d+", value):
            return None
        value = int(value)
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0 and value.is_integer():
        return int(value)
    return None


def parse_score_count_from_text(text: str, label_pattern: str) -> int | None:
    patterns = [
        rf"\bBased\s+on\s+([\d,]+)\s+{label_pattern}\b",
        rf"\bShowing\s+([\d,]+)\s+{label_pattern}\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return normalize_count(match.group(1))
    return None


def parse_score_count_text(page: str, label_pattern: str) -> int | None:
    return parse_score_count_from_text(clean_html_text(page), label_pattern)


def normalized_page_text(page: str) -> str:
    return re.sub(r"\s+", " ", clean_html_text(page)).strip()


def parse_metacritic_product_summary_values(
    page: str,
) -> tuple[int | None, int | None, float | None, int | None]:
    text = normalized_page_text(page)
    score = review_count = user_score = user_count = None

    critic_match = re.search(
        r"\bMetascore\b(?:(?!\bUser score\b).){0,500}?"
        r"\bBased on ([\d,]+) Critic Reviews?\s+(\d{1,3})\b",
        text,
        re.I | re.S,
    )
    if critic_match:
        review_count = normalize_count(critic_match.group(1))
        raw_score = int(critic_match.group(2))
        if 0 <= raw_score <= 100:
            score = raw_score

    user_match = re.search(
        r"\bUser score\b(?:(?!\bMy Score\b).){0,500}?"
        r"\bBased on ([\d,]+) User (?:Ratings?|Reviews?)\s+"
        r"(10(?:\.0)?|[0-9](?:\.\d+)?)\b",
        text,
        re.I | re.S,
    )
    if user_match:
        user_count = normalize_count(user_match.group(1))
        user_score = normalize_metacritic_user_score(user_match.group(2))

    score, review_count = sanitize_metacritic_score(score, review_count)
    user_score, user_count = sanitize_user_score(user_score, user_count, score)
    return score, review_count, user_score, user_count


def parse_user_score_value(text: str) -> float | None:
    text = text.strip()
    if re.fullmatch(r"(?:100(?:\.0)?|[0-9]{1,2}(?:\.\d+)?)", text):
        return normalize_metacritic_user_score(text)
    if re.search(r"\btbd\b", text, re.I):
        return None
    decimal_scores = re.findall(r"\b(?:10\.0|[0-9]\.\d+)\b", text)
    if decimal_scores:
        return normalize_metacritic_user_score(decimal_scores[-1])
    return None


def parse_visible_user_score_value(text: str) -> float | None:
    text = text.strip()
    if re.fullmatch(r"(?:10(?:\.0)?|[0-9](?:\.\d+)?)", text):
        return normalize_metacritic_user_score(text)
    if re.search(r"\btbd\b", text, re.I):
        return None
    decimal_scores = re.findall(r"\b(?:10\.0|[0-9]\.\d+)\b", text)
    if decimal_scores:
        return normalize_metacritic_user_score(decimal_scores[-1])
    return None


def clean_html_text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment))


def parse_metacritic_visible_score(page: str) -> int | None:
    label_matches = re.finditer(
        r'\b(?:aria-label|title)=["\']Metascore\s+(\d{1,3})\s+out\s+of\s+100["\']',
        page,
        re.I,
    )
    for match in label_matches:
        score = int(match.group(1))
        if 0 <= score <= 100:
            return score

    wrappers = re.finditer(
        r'<[^>]*(?=[^>]*\bdata-testid=["\']global-score-value-wrapper["\'])[^>]*>',
        page,
        re.I,
    )
    for wrapper in wrappers:
        label_match = re.search(
            r'\b(?:aria-label|title)=["\']Metascore\s+(\d{1,3})\s+out\s+of\s+100["\']',
            wrapper.group(0),
            re.I,
        )
        if label_match:
            score = int(label_match.group(1))
            if 0 <= score <= 100:
                return score

    spans = re.finditer(
        r'<span\b(?=[^>]*\bdata-testid=["\']global-score-value["\'])[^>]*>(.*?)</span>',
        page,
        re.I | re.S,
    )
    for match in spans:
        raw_score = clean_html_text(match.group(1)).strip()
        if not re.fullmatch(r"\d{1,3}", raw_score):
            continue
        score = int(raw_score)
        if not 0 <= score <= 100:
            continue
        prefix = clean_html_text(page[max(0, match.start() - 4000) : match.start()]).lower()
        critic_label = max(prefix.rfind("metascore"), prefix.rfind("critic score"))
        user_label = prefix.rfind("user score")
        if critic_label > user_label:
            return score
    return None


def parse_metacritic_global_score_user_score(page: str) -> float | None:
    label_matches = re.finditer(
        r'\b(?:aria-label|title)=["\']User\s+Score\s+'
        r'(10(?:\.0)?|[0-9](?:\.\d+)?)\s+out\s+of\s+10["\']',
        page,
        re.I,
    )
    for match in label_matches:
        return normalize_metacritic_user_score(match.group(1))

    wrappers = re.finditer(
        r'<[^>]*(?=[^>]*\bdata-testid=["\']global-score-value-wrapper["\'])[^>]*>',
        page,
        re.I,
    )
    for wrapper in wrappers:
        label_match = re.search(
            r'\b(?:aria-label|title)=["\']User\s+Score\s+'
            r'(10(?:\.0)?|[0-9](?:\.\d+)?)\s+out\s+of\s+10["\']',
            wrapper.group(0),
            re.I,
        )
        if label_match:
            return normalize_metacritic_user_score(label_match.group(1))

    spans = re.finditer(
        r'<span\b(?=[^>]*\bdata-testid=["\']global-score-value["\'])[^>]*>(.*?)</span>',
        page,
        re.I | re.S,
    )
    for match in spans:
        score = parse_visible_user_score_value(clean_html_text(match.group(1)))
        if score is None:
            continue
        prefix = clean_html_text(page[max(0, match.start() - 4000) : match.start()]).lower()
        user_label = prefix.rfind("user score")
        critic_label = max(prefix.rfind("critic score"), prefix.rfind("metascore"))
        if user_label > critic_label:
            return score

    return None


def parse_metacritic_score(page: str, expected_slug: str | None = None) -> int | None:
    nuxt_score = parse_metacritic_nuxt_score(page, expected_slug)
    if nuxt_score is not None:
        return nuxt_score
    visible_score = parse_metacritic_visible_score(page)
    if visible_score is not None:
        return visible_score
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


def parse_metacritic_review_count(page: str, expected_slug: str | None = None) -> int | None:
    nuxt_count = parse_metacritic_nuxt_count(page, "criticScoreSummary", "reviewCount", expected_slug)
    if nuxt_count is not None:
        return nuxt_count
    return parse_score_count_text(page, "Critic Reviews?")


def parse_metacritic_user_score(page: str, expected_slug: str | None = None) -> float | None:
    nuxt_score = parse_metacritic_nuxt_user_score(page, expected_slug)
    if nuxt_score is not None:
        return nuxt_score
    return parse_metacritic_visible_user_score(page)


def parse_metacritic_user_rating_count(page: str, expected_slug: str | None = None) -> int | None:
    nuxt_count = parse_metacritic_nuxt_count(page, "userScoreSummary", "ratingCount", expected_slug)
    if nuxt_count is not None:
        return nuxt_count
    return parse_score_count_text(page, r"User (?:Ratings?|Reviews?)")


def parse_metacritic_nuxt_data(page: str) -> list[Any] | None:
    match = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not match:
        return None
    try:
        data = json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def resolve_metacritic_nuxt(data: list[Any], value: Any) -> Any:
    if isinstance(value, int) and 0 <= value < len(data):
        return data[value]
    return value


def parse_metacritic_nuxt_rating_values(
    data: list[Any] | None,
    expected_slug: str | None = None,
) -> tuple[int | None, int | None, float | None, int | None]:
    if data is None:
        return None, None, None, None
    score = review_count = user_score = user_count = None
    for item in data:
        if not isinstance(item, dict) or "title" not in item:
            continue
        item_type = resolve_metacritic_nuxt(data, item.get("type"))
        if isinstance(item_type, str) and not item_type.endswith("-title"):
            continue
        slug = resolve_metacritic_nuxt(data, item.get("slug"))
        if expected_slug and slug != expected_slug:
            continue

        critic_summary = resolve_metacritic_nuxt(data, item.get("criticScoreSummary"))
        if isinstance(critic_summary, dict):
            raw_score = resolve_metacritic_nuxt(data, critic_summary.get("score"))
            if isinstance(raw_score, (int, float)) and 0 <= raw_score <= 100:
                score = int(round(raw_score))
            review_count = normalize_count(resolve_metacritic_nuxt(data, critic_summary.get("reviewCount")))

        user_summary = resolve_metacritic_nuxt(data, item.get("userScoreSummary"))
        if isinstance(user_summary, dict):
            raw_user_score = resolve_metacritic_nuxt(data, user_summary.get("score"))
            raw_user_count = resolve_metacritic_nuxt(data, user_summary.get("ratingCount"))
            candidate_user_score = normalize_metacritic_user_score(raw_user_score)
            candidate_user_count = normalize_count(raw_user_count)
            if (
                candidate_user_score is not None
                and candidate_user_count is None
                and normalize_count(raw_user_score) is not None
                and normalize_count(raw_user_score) > 10
            ):
                candidate_user_score = None
            user_score = candidate_user_score
            user_count = candidate_user_count

        if score is not None or review_count is not None or user_score is not None or user_count is not None:
            return score, review_count, user_score, user_count
    return None, None, None, None


def parse_metacritic_rating_values(
    page: str,
    expected_slug: str | None = None,
    prefer_visible: bool = False,
) -> tuple[int | None, int | None, float | None, int | None]:
    if prefer_visible:
        score = parse_metacritic_visible_score(page)
        user_score = parse_metacritic_visible_user_score(page)
        text = clean_html_text(page)
        review_count = parse_score_count_from_text(text, "Critic Reviews?")
        user_count = parse_score_count_from_text(text, r"User (?:Ratings?|Reviews?)")
        if score is None and review_count is None and user_score is None and user_count is None:
            nuxt_data = parse_metacritic_nuxt_data(page)
            score, review_count, user_score, user_count = parse_metacritic_nuxt_rating_values(nuxt_data, expected_slug)
        if score is not None and metacritic_critic_reviews_unavailable(text):
            score = None
            review_count = None
        score, review_count = sanitize_metacritic_score(score, review_count)
        user_score, user_count = sanitize_user_score(user_score, user_count, score)
        return score, review_count, user_score, user_count

    score, review_count, user_score, user_count = parse_metacritic_product_summary_values(page)

    if score is None and review_count is None and user_score is None and user_count is None:
        nuxt_data = parse_metacritic_nuxt_data(page)
        score, review_count, user_score, user_count = parse_metacritic_nuxt_rating_values(nuxt_data, expected_slug)

    if score is None:
        score = parse_metacritic_visible_score(page)
    if user_score is None:
        user_score = parse_metacritic_visible_user_score(page)
    if review_count is None or user_count is None:
        text = clean_html_text(page)
        if review_count is None:
            review_count = parse_score_count_from_text(text, "Critic Reviews?")
        if user_count is None:
            user_count = parse_score_count_from_text(text, r"User (?:Ratings?|Reviews?)")
        if score is not None and metacritic_critic_reviews_unavailable(text):
            score = None
            review_count = None
    score, review_count = sanitize_metacritic_score(score, review_count)
    user_score, user_count = sanitize_user_score(user_score, user_count, score)
    return score, review_count, user_score, user_count


def absolute_metacritic_url(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return urllib.parse.urljoin(METACRITIC_BASE_URL, url)


def parse_metacritic_platform_product_card(
    page: str,
    platform: str | None,
) -> tuple[int | None, str | None, int | None]:
    if not platform:
        return None, None, None
    expected_platform = metacritic_platform_slug_from_label(platform) or platform
    cards = re.finditer(
        r"<a\b(?=[^>]*\bdata-testid=[\"']product-score-card[\"'])"
        r"(?P<attrs>[^>]*)>(?P<body>.*?)</a>",
        page,
        re.I | re.S,
    )
    for card in cards:
        attrs = card.group("attrs")
        body = card.group("body")
        href_match = re.search(r"\bhref=[\"'](?P<href>[^\"']+)[\"']", attrs, re.I)
        if href_match is None or "/critic-reviews/" not in html.unescape(href_match.group("href")):
            continue
        href = html.unescape(href_match.group("href"))
        parsed_href = urllib.parse.urlsplit(href)
        query_platform = urllib.parse.parse_qs(parsed_href.query).get("platform", [None])[0]
        card_platform = metacritic_platform_slug_from_label(query_platform or "")
        if card_platform is None:
            card_platforms = parse_metacritic_page_platforms(body)
            card_platform = next(iter(card_platforms), None) if len(card_platforms) == 1 else None
        if card_platform != expected_platform:
            continue

        score_match = re.search(r"\b(?:title|aria-label)=[\"']Metascore\s+(\d{1,3})\s+out of 100[\"']", body, re.I)
        count_match = re.search(r"Based on\s+([\d,]+)\s+Critic Reviews?", clean_html_text(body), re.I)
        score = int(score_match.group(1)) if score_match else None
        review_count = normalize_count(count_match.group(1)) if count_match else None
        score, review_count = sanitize_metacritic_score(score, review_count)
        return score, absolute_metacritic_url(href) if score is not None else None, review_count
    return None, None, None


def parse_metacritic_nuxt_count(
    page: str,
    summary_key: str,
    count_key: str,
    expected_slug: str | None = None,
) -> int | None:
    data = parse_metacritic_nuxt_data(page)
    if data is None:
        return None

    for item in data:
        if not isinstance(item, dict) or summary_key not in item or "title" not in item:
            continue
        item_type = resolve_metacritic_nuxt(data, item.get("type"))
        if isinstance(item_type, str) and not item_type.endswith("-title"):
            continue
        slug = resolve_metacritic_nuxt(data, item.get("slug"))
        if expected_slug and slug != expected_slug:
            continue
        summary = resolve_metacritic_nuxt(data, item[summary_key])
        if not isinstance(summary, dict):
            continue
        count = normalize_count(resolve_metacritic_nuxt(data, summary.get(count_key)))
        if count is not None:
            return count
    return None


def parse_metacritic_nuxt_score(page: str, expected_slug: str | None = None) -> int | None:
    score, _review_count, _user_score, _user_count = parse_metacritic_nuxt_rating_values(
        parse_metacritic_nuxt_data(page),
        expected_slug,
    )
    return score


def parse_metacritic_nuxt_user_score(page: str, expected_slug: str | None = None) -> float | None:
    _score, _review_count, user_score, _user_count = parse_metacritic_nuxt_rating_values(
        parse_metacritic_nuxt_data(page),
        expected_slug,
    )
    return user_score


def parse_metacritic_visible_user_score(page: str) -> float | None:
    global_score = parse_metacritic_global_score_user_score(page)
    if global_score is not None:
        return global_score

    text = html.unescape(re.sub(r"<[^>]+>", "\n", page))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if not re.search(r"\bUser Score\b", line, re.I):
            continue
        inline_score = parse_visible_user_score_value(line)
        if inline_score is not None:
            return inline_score
        for candidate in lines[index + 1 : index + 8]:
            if re.search(
                r"\b(?:My Score|Critic Reviews|User Reviews|Metascore|"
                r"positive|mixed|negative|Add My Review|Showing)\b",
                candidate,
                re.I,
            ):
                break
            score = parse_visible_user_score_value(candidate)
            if score is not None:
                return score
            if re.fullmatch(r"tbd", candidate, re.I):
                return None
    return None


def parse_metacritic_search_slugs(page: str, query: str, limit: int = METACRITIC_SEARCH_SLUG_LIMIT) -> list[str]:
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
        review_count = normalize_count(resolve(summary.get("reviewCount")) if isinstance(summary, dict) else None)
        if isinstance(score, (int, float)):
            score, review_count = sanitize_metacritic_score(int(round(score)), review_count)
        user_summary = resolve(item.get("userScoreSummary"))
        user_score = normalize_metacritic_user_score(
            resolve(user_summary.get("score")) if isinstance(user_summary, dict) else None,
        )
        user_rating_count = normalize_count(
            resolve(user_summary.get("ratingCount")) if isinstance(user_summary, dict) else None,
        )
        user_score, user_rating_count = sanitize_user_score(
            user_score,
            user_rating_count,
            int(round(score)) if isinstance(score, (int, float)) else None,
        )
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
                metacritic=int(score),
                metacritic_url=f"{METACRITIC_BASE_URL}/game/{slug}/",
                updated_at=time.time(),
                review_count=review_count,
                user_score=user_score,
                user_score_url=f"{METACRITIC_BASE_URL}/game/{slug}/user-reviews/" if user_score is not None else None,
                user_rating_count=user_rating_count,
                user_score_updated_at=time.time() if user_score is not None else None,
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
    fetch_start = time.perf_counter()
    page = fetch_text(url, timeout)
    fetch_elapsed = time.perf_counter() - fetch_start
    parse_start = time.perf_counter()
    entries = parse_metacritic_browse_entries(page)
    parse_elapsed = time.perf_counter() - parse_start
    debug_log(
        f"[debug] catalog page {page_number}: "
        f"fetch={format_seconds(fetch_elapsed)}, parse={format_seconds(parse_elapsed)}, entries={len(entries)}"
    )
    return entries


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

    progress = create_progress()
    catalog_task = None
    if progress is not None:
        progress.start()
        catalog_task = progress.add_task(
            f"Metacritic catalog pages ({platform})",
            total=None,
            detail="page 1",
        )
    try:
        first_url = metacritic_browse_url(platform, 1)
        log(f"  Metacritic platform page: {first_url}")
        first_fetch_start = time.perf_counter()
        first_page = fetch_text(first_url, timeout)
        first_fetch_elapsed = time.perf_counter() - first_fetch_start
        pages_parse_start = time.perf_counter()
        total_pages = parse_metacritic_total_pages(first_page)
        pages_parse_elapsed = time.perf_counter() - pages_parse_start
        if max_pages is not None:
            total_pages = min(total_pages, max_pages)
        if catalog_task is not None:
            progress.update(
                catalog_task,
                total=max(total_pages, 1),
                completed=1,
                detail=f"page 1/{max(total_pages, 1)}",
            )

        first_parse_start = time.perf_counter()
        first_entries = parse_metacritic_browse_entries(first_page) if total_pages >= 1 else []
        first_parse_elapsed = time.perf_counter() - first_parse_start
        debug_log(
            f"[debug] catalog page 1: fetch={format_seconds(first_fetch_elapsed)}, "
            f"pagination_parse={format_seconds(pages_parse_elapsed)}, "
            f"parse={format_seconds(first_parse_elapsed)}, entries={len(first_entries)}"
        )

        page_results: dict[int, list[PlatformRating]] = {1: first_entries}
        page_numbers = list(range(2, total_pages + 1))

        worker_count = max(1, workers)
        if worker_count == 1:
            for page_number in page_numbers:
                if delay:
                    time.sleep(delay)
                page_results[page_number] = fetch_metacritic_platform_page(platform, page_number, timeout)
                if catalog_task is not None:
                    progress.update(
                        catalog_task,
                        completed=page_number,
                        detail=f"page {page_number}/{total_pages}",
                    )
        elif page_numbers:
            if delay:
                log("--delay is ignored for Metacritic platform catalog pages when --workers is greater than 1.")
            with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(fetch_metacritic_platform_page, platform, page_number, timeout): page_number
                    for page_number in page_numbers
                }
                completed_pages = 1
                for future in concurrent.futures.as_completed(futures):
                    page_number = futures[future]
                    page_results[page_number] = future.result()
                    completed_pages += 1
                    if catalog_task is not None:
                        progress.update(
                            catalog_task,
                            completed=completed_pages,
                            detail=f"page {page_number}/{total_pages}",
                        )
    finally:
        if progress is not None:
            progress.stop()

    for page_number in sorted(page_results):
        for entry in page_results[page_number]:
            if entry.slug in seen:
                continue
            seen.add(entry.slug)
            ratings.append(entry)
    return ratings


def search_metacritic_slugs(
    title: str,
    timeout: float,
    timings: list[tuple[str, float]] | None = None,
) -> list[str]:
    timings = timings if timings is not None else []
    seen: set[str] = set()
    slugs = []
    for variant in title_variants(title):
        url = f"https://www.metacritic.com/search/{urllib.parse.quote(variant)}/"
        try:
            log(f"  Metacritic search: {url}")
            page = debug_fetch_text(url, timeout, timings, "mc search fetch") if DEBUG else fetch_text(url, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        parse_start = time.perf_counter()
        parsed_slugs = parse_metacritic_search_slugs(page, variant)
        if DEBUG:
            timings.append(("mc search parse", time.perf_counter() - parse_start))
        for slug in parsed_slugs:
            if slug not in seen:
                seen.add(slug)
                slugs.append(slug)
    return slugs


def metacritic_platform_query_values(platform: str | None) -> list[str]:
    if not platform:
        return []
    mapped = METACRITIC_REVIEW_PLATFORM_PARAMS.get(platform, platform)
    return list(dict.fromkeys([mapped, platform]))


def metacritic_urls(kind: str, slug: str, platform: str | None) -> list[str]:
    urls = []
    urls.append(f"https://www.metacritic.com/{kind}/{slug}/")
    if kind == "game" and platform:
        for query_platform in metacritic_platform_query_values(platform):
            urls.append(f"https://www.metacritic.com/game/{slug}/critic-reviews/?platform={query_platform}")
            urls.append(f"https://www.metacritic.com/game/{slug}/user-reviews/?platform={query_platform}")
    return urls


def metacritic_review_urls(
    slug: str,
    platform: str | None,
    include_critic: bool = True,
    include_user: bool = True,
) -> list[str]:
    urls = []
    for query_platform in metacritic_platform_query_values(platform):
        if include_critic:
            urls.append(f"https://www.metacritic.com/game/{slug}/critic-reviews/?platform={query_platform}")
        if include_user:
            urls.append(f"https://www.metacritic.com/game/{slug}/user-reviews/?platform={query_platform}")
    return urls


def is_metacritic_review_url(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    return path.endswith("/critic-reviews") or path.endswith("/user-reviews")


def metacritic_user_score_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/critic-reviews"):
        path = path[: -len("/critic-reviews")]
    if not path.endswith("/user-reviews"):
        path = f"{path}/user-reviews"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, f"{path}/", parsed.query, ""))


def get_metacritic(
    title: str,
    kind: str,
    platform: str | None,
    timeout: float,
    timings: list[tuple[str, float]] | None = None,
    excluded_slugs: set[str] | None = None,
) -> tuple[int | None, str | None, int | None, float | None, str | None, int | None, str]:
    timings = timings if timings is not None else []
    excluded_slugs = excluded_slugs or set()
    saw_page = False
    saw_404 = False
    searched = False
    slugs = [slug for slug in slug_candidates(title) if slug not in excluded_slugs]
    for slug in slugs:
        result = get_metacritic_slug(slug, kind, platform, timeout, timings)
        if result[-1] == "ok":
            return result
        if result[-1] == "not found":
            saw_page = True
        elif result[-1] == "metacritic 404":
            saw_404 = True

    if kind == "game" and (saw_page or saw_404 or excluded_slugs):
        searched = True
        for slug in search_metacritic_slugs(title, timeout, timings):
            if slug in slugs or slug in excluded_slugs:
                continue
            result = get_metacritic_slug(slug, kind, platform, timeout, timings)
            if result[-1] == "ok":
                return result
            if result[-1] == "not found":
                saw_page = True
            elif result[-1] == "metacritic 404":
                saw_404 = True

    if saw_page:
        return None, None, None, None, None, None, "not found"
    return None, None, None, None, None, None, "metacritic 404" if searched or saw_404 else "not found"


def get_metacritic_slug(
    slug: str,
    kind: str,
    platform: str | None,
    timeout: float,
    timings: list[tuple[str, float]] | None = None,
) -> tuple[int | None, str | None, int | None, float | None, str | None, int | None, str]:
    timings = timings if timings is not None else []
    saw_page = False
    saw_404 = False
    score = review_count = user_score = user_count = None
    score_url = user_score_url = None

    def result(status: str) -> tuple[int | None, str | None, int | None, float | None, str | None, int | None, str]:
        return score, score_url, review_count, user_score, user_score_url, user_count, status

    def merge_page_values(url: str, page: str, review_url: bool) -> None:
        nonlocal score, review_count, user_score, user_count, score_url, user_score_url
        current_score, current_review_count, current_user_score, current_user_count = parse_metacritic_rating_values(
            page,
            slug,
            prefer_visible=bool(kind == "game" and platform and review_url),
        )
        if current_score is not None:
            score = current_score
            score_url = url
        elif current_user_score is not None and score_url is None:
            score_url = url
        if current_review_count is not None:
            review_count = current_review_count
        if current_user_score is not None:
            user_score = current_user_score
            user_score_url = metacritic_user_score_url(url)
        if current_user_count is not None:
            user_count = current_user_count

    product_url = f"https://www.metacritic.com/{kind}/{slug}/"
    urls: list[str] = [product_url]
    review_urls: list[str] = []

    for url in urls:
        try:
            log(f"  Metacritic product page: {url}")
            page = debug_fetch_text(url, timeout, timings, "mc product fetch") if DEBUG else fetch_text(url, timeout)
            saw_page = True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                saw_404 = True
                log("  Metacritic: 404, trying next URL if available")
                if kind == "game" and platform:
                    review_urls = metacritic_review_urls(slug, platform)
                continue
            raise
        parse_start = time.perf_counter()
        page_matches_platform = True
        if kind == "game" and platform:
            page_matches_platform = metacritic_product_page_matches_single_platform(page, platform)
        if page_matches_platform:
            merge_page_values(url, page, review_url=False)
        if DEBUG:
            timings.append(("mc product parse", time.perf_counter() - parse_start))
        if not page_matches_platform:
            card_score, card_score_url, card_review_count = parse_metacritic_platform_product_card(page, platform)
            if card_score is not None:
                score = card_score
                score_url = card_score_url
            if card_review_count is not None:
                review_count = card_review_count
            log(f"  Metacritic: product page is ambiguous for {platform}, checking platform review pages")
            review_urls = metacritic_review_urls(slug, platform)
        elif kind == "game" and platform:
            need_critic = score is None or review_count is None
            need_user = user_score is None or user_count is None
            if not need_critic and not need_user:
                return result("ok")
            review_urls = metacritic_review_urls(
                slug,
                platform,
                include_critic=need_critic,
                include_user=need_user,
            )
        elif score is not None or user_score is not None:
            return result("ok")

    for url in review_urls:
        try:
            log(f"  Metacritic product page: {url}")
            page = debug_fetch_text(url, timeout, timings, "mc product fetch") if DEBUG else fetch_text(url, timeout)
            saw_page = True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                saw_404 = True
                log("  Metacritic: 404, trying next URL if available")
                continue
            raise
        parse_start = time.perf_counter()
        if metacritic_page_matches_platform(page, platform):
            merge_page_values(url, page, review_url=True)
        elif DEBUG:
            debug_log(f"[debug] review page platform mismatch for {slug!r}: {url}")
        if DEBUG:
            timings.append(("mc product parse", time.perf_counter() - parse_start))
        if score is not None and user_score is not None:
            return result("ok")

    if score is not None or user_score is not None:
        return result("ok")
    if saw_page:
        return None, None, None, None, None, None, "not found"
    return None, None, None, None, None, None, "metacritic 404" if saw_404 else "not found"


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
            if rating.review_count is None:
                rating.review_count = previous.review_count
            if previous.product_page_parsed:
                rating.product_page_parsed = True
            if previous.user_score is not None:
                rating.user_score = previous.user_score
                rating.user_score_url = previous.user_score_url
                rating.user_rating_count = previous.user_rating_count
                rating.user_score_updated_at = previous.user_score_updated_at
            elif rating.user_rating_count is None:
                rating.user_rating_count = previous.user_rating_count
        merged.append(rating)
    return merged


def save_platform_ratings(
    path: Path,
    platform: str,
    ratings: list[PlatformRating],
    created_at: float | None = None,
    updated_at: float | None = None,
) -> None:
    with CACHE_WRITE_LOCK:
        payload = load_system_platform_cache(path)
        platform_cache = payload.get("platforms", {})
        if not isinstance(platform_cache, dict):
            platform_cache = {}
        existing_platform = platform_cache.get(platform, {})
        filename_matches = existing_platform.get("filename_matches", {}) if isinstance(existing_platform, dict) else {}
        if not isinstance(filename_matches, dict):
            filename_matches = {}
        existing_created = existing_platform.get("created_at") if isinstance(existing_platform, dict) else None
        created = (
            created_at
            or (float(existing_created) if isinstance(existing_created, (int, float)) else None)
            or time.time()
        )
        updated = updated_at or created
        platform_cache[platform] = {
            "platform": platform,
            "created_at": created,
            "updated_at": updated,
            "items": [dataclasses.asdict(rating) for rating in ratings],
            "filename_matches": filename_matches,
        }
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "platforms": platform_cache,
        }
        atomic_write_json(path, payload)


def load_filename_matches(path: Path, platform: str) -> dict[str, FilenameMatch]:
    payload = load_system_platform_cache(path)
    platforms = payload.get("platforms", {})
    if not isinstance(platforms, dict):
        return {}
    platform_data = platforms.get(platform, {})
    if not isinstance(platform_data, dict):
        return {}
    raw_matches = platform_data.get("filename_matches", {})
    if not isinstance(raw_matches, dict):
        return {}
    platform_created_at = platform_data.get("created_at") or platform_data.get("updated_at")
    platform_created = float(platform_created_at) if isinstance(platform_created_at, (int, float)) else None
    loaded_matches: list[tuple[str, FilenameMatch]] = []
    for key, value in raw_matches.items():
        if isinstance(value, dict):
            try:
                filename_match = FilenameMatch.from_dict(value)
            except (KeyError, TypeError, ValueError):
                continue
            if (
                filename_match.status == "not found"
                and platform_created is not None
                and filename_match.updated_at < platform_created
            ):
                continue
            if (
                filename_match.lookup_version < FILENAME_MATCH_LOOKUP_VERSION
                and filename_match.match_type == "miss"
            ):
                continue
            loaded_matches.append((str(key), filename_match))

    matches: dict[str, FilenameMatch] = {}
    for key, filename_match in loaded_matches:
        if filename_match_prefer(filename_match, matches.get(key)):
            matches[key] = filename_match

    for key, filename_match in loaded_matches:
        for alias_key in legacy_filename_match_alias_keys(key):
            if filename_match_prefer(filename_match, matches.get(alias_key)):
                matches[alias_key] = filename_match
    return matches


def save_filename_matches(path: Path, platform: str, matches: dict[str, FilenameMatch]) -> None:
    with CACHE_WRITE_LOCK:
        payload = load_system_platform_cache(path)
        platforms = payload.get("platforms", {})
        if not isinstance(platforms, dict):
            platforms = {}
        platform_data = platforms.get(platform, {})
        if not isinstance(platform_data, dict):
            platform_data = {"platform": platform, "created_at": time.time(), "updated_at": time.time(), "items": []}
        platform_data["filename_matches"] = {
            key: dataclasses.asdict(value) for key, value in sorted(matches.items())
        }
        platform_data["updated_at"] = time.time()
        platforms[platform] = platform_data
        payload = {"version": 1, "updated_at": time.time(), "platforms": platforms}
        atomic_write_json(path, payload)


def match_review_fieldnames() -> list[str]:
    return ["local_title", "candidate_title", "candidate_slug", "score", "reason", "decision"]


def collect_match_review_candidates(ratings: list[Rating]) -> list[MatchReviewCandidate]:
    seen: set[tuple[str, str]] = set()
    candidates = []
    for rating in ratings:
        for candidate in rating_match_review_candidates(rating):
            key = (filename_match_key(candidate.local_title), candidate.candidate_slug)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
    return sorted(candidates, key=lambda candidate: (candidate.local_title.lower(), -candidate.score, candidate.candidate_title.lower()))


def write_match_review(path: Path, candidates: list[MatchReviewCandidate]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as review_file:
        writer = csv.DictWriter(review_file, fieldnames=match_review_fieldnames(), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(
                {
                    "local_title": candidate.local_title,
                    "candidate_title": candidate.candidate_title,
                    "candidate_slug": candidate.candidate_slug,
                    "score": f"{candidate.score:.4f}",
                    "reason": candidate.reason,
                    "decision": "",
                }
            )


def add_rejected_slug(match: FilenameMatch, slug: str) -> None:
    if slug not in match.rejected_slugs:
        match.rejected_slugs.append(slug)
        match.rejected_slugs.sort()


def apply_match_review(
    path: Path,
    matches: dict[str, FilenameMatch],
    platform_ratings: list[PlatformRating] | None,
) -> bool:
    path = path.expanduser().resolve()
    ratings_by_slug = {rating.slug: rating for rating in platform_ratings or []}
    changed = False
    with path.open("r", encoding="utf-8", newline="") as review_file:
        reader = csv.DictReader(review_file, delimiter="\t")
        for row in reader:
            local_title = (row.get("local_title") or "").strip()
            candidate_slug = (row.get("candidate_slug") or "").strip()
            decision = (row.get("decision") or "").strip().lower()
            if not local_title or not candidate_slug or decision not in {"accept", "reject"}:
                continue
            key = filename_match_key(local_title)
            existing = matches.get(key)
            if decision == "accept":
                platform_rating = ratings_by_slug.get(candidate_slug)
                new_match = FilenameMatch(
                    title=local_title,
                    slug=candidate_slug,
                    status="ok",
                    match_type="manual",
                    metacritic=platform_rating.metacritic if platform_rating is not None else None,
                    metacritic_url=platform_rating.metacritic_url if platform_rating is not None else None,
                    review_count=platform_rating.review_count if platform_rating is not None else None,
                    user_score=platform_rating.user_score if platform_rating is not None else None,
                    user_score_url=platform_rating.user_score_url if platform_rating is not None else None,
                    user_rating_count=platform_rating.user_rating_count if platform_rating is not None else None,
                    product_page_parsed=platform_rating.product_page_parsed if platform_rating is not None else False,
                    rejected_slugs=[
                        slug
                        for slug in (existing.rejected_slugs if existing is not None else [])
                        if slug != candidate_slug
                    ],
                )
                if existing != new_match:
                    matches[key] = new_match
                    changed = True
                continue

            if existing is None:
                existing = FilenameMatch(
                    title=local_title,
                    slug=None,
                    status="reviewed",
                    match_type="manual-reject",
                )
                matches[key] = existing
                changed = True
            elif existing.status == "ok" and existing.slug == candidate_slug:
                existing.slug = None
                existing.status = "reviewed"
                existing.match_type = "manual-reject"
                existing.metacritic = None
                existing.metacritic_url = None
                existing.review_count = None
                existing.user_score = None
                existing.user_score_url = None
                existing.user_rating_count = None
                existing.product_page_parsed = False
                changed = True
            before = list(existing.rejected_slugs)
            add_rejected_slug(existing, candidate_slug)
            existing.updated_at = time.time()
            if existing.rejected_slugs != before:
                changed = True
    return changed


def iter_games(folder: Path, extensions: set[str], include_dirs: bool) -> list[Path]:
    entries: list[Path] = []
    for child in folder.iterdir():
        if child.name == LEGACY_CACHE_NAME or child.name.startswith("."):
            continue
        if child.is_file() and child.suffix.lower() in extensions:
            entries.append(child)
        elif include_dirs and child.is_dir():
            entries.append(child)
    return sorted(entries, key=lambda p: p.name.lower())


def platform_match_candidates(
    title: str,
    ratings: list[PlatformRating],
    rejected_slugs: set[str] | None = None,
    limit: int | None = None,
) -> list[PlatformMatchCandidate]:
    if not ratings:
        return []
    rejected_slugs = rejected_slugs or set()
    candidates_by_slug: dict[str, PlatformMatchCandidate] = {}

    by_slug = {rating.slug: rating for rating in ratings}
    for slug in slug_candidates(title):
        match = by_slug.get(slug)
        if match is not None and match.slug not in rejected_slugs:
            candidates_by_slug[match.slug] = PlatformMatchCandidate(match, 1.0, "exact-slug")

    title_keys = title_match_keys(title)
    title_tokens = normalized_tokens(title)
    title_stripped = normalize_context_name(strip_trailing_version_token(title))
    for rating in ratings:
        if rating.slug in rejected_slugs:
            continue
        version_conflict = has_version_conflict(title, rating.title)
        rating_keys = title_match_keys(rating.title)
        if title_keys & rating_keys:
            candidate = PlatformMatchCandidate(rating, 1.0, "exact-title", version_conflict)
            candidates_by_slug[rating.slug] = candidate
            continue

        score = title_similarity(title, rating.title)
        reason = "fuzzy"
        rating_tokens = normalized_tokens(rating.title)
        if title_tokens and title_tokens <= rating_tokens and meaningful_extra_tokens(title_tokens, rating_tokens):
            if score < 0.90:
                reason = "local-title-subset"
            score = max(score, 0.90)
        elif rating_tokens and rating_tokens <= title_tokens and meaningful_extra_tokens(rating_tokens, title_tokens):
            if score < 0.88:
                reason = "catalog-title-subset"
            score = max(score, 0.88)

        rating_stripped = normalize_context_name(strip_trailing_version_token(rating.title))
        if title_stripped and title_stripped == rating_stripped:
            if score < 0.80:
                reason = "version-stripped-title"
            score = max(score, 0.80)

        if score >= REVIEW_MATCH_THRESHOLD:
            if version_conflict:
                reason = f"{reason}; version-conflict"
            old_candidate = candidates_by_slug.get(rating.slug)
            new_candidate = PlatformMatchCandidate(rating, score, reason, version_conflict)
            if old_candidate is None or new_candidate.score > old_candidate.score:
                candidates_by_slug[rating.slug] = new_candidate

    candidates = sorted(
        candidates_by_slug.values(),
        key=lambda candidate: (-candidate.score, candidate.rating.title.lower(), candidate.rating.slug),
    )
    return candidates[:limit] if limit is not None else candidates


def auto_platform_rating_match(title: str, ratings: list[PlatformRating], rejected_slugs: set[str] | None = None) -> tuple[PlatformRating, str, PlatformMatchCandidate] | None:
    for candidate in platform_match_candidates(title, ratings, rejected_slugs):
        if candidate.reason in {"exact-slug", "exact-title"}:
            return candidate.rating, candidate.reason, candidate
        if candidate.score >= AUTO_MATCH_THRESHOLD and not candidate.version_conflict:
            return candidate.rating, "fuzzy", candidate
    return None


def review_platform_match_candidates(
    title: str,
    ratings: list[PlatformRating],
    rejected_slugs: set[str] | None = None,
) -> list[PlatformMatchCandidate]:
    review_candidates = []
    for candidate in platform_match_candidates(title, ratings, rejected_slugs, MAX_REVIEW_CANDIDATES):
        if candidate.reason in {"exact-slug", "exact-title"}:
            continue
        if candidate.score >= AUTO_MATCH_THRESHOLD and not candidate.version_conflict:
            continue
        if candidate.score >= REVIEW_MATCH_THRESHOLD:
            review_candidates.append(candidate)
    return review_candidates


def platform_rating_match(title: str, ratings: list[PlatformRating]) -> tuple[PlatformRating, str] | None:
    match = auto_platform_rating_match(title, ratings)
    if match is None:
        return None
    rating, match_type, _candidate = match
    return rating, match_type


def best_platform_rating_match(title: str, ratings: list[PlatformRating]) -> PlatformRating | None:
    match = platform_rating_match(title, ratings)
    return match[0] if match else None


def filename_match_key(title: str) -> str:
    return slugify(normalize_context_name(title))


def platform_rating_slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/game/([^/?#]+)/?", url)
    return match.group(1) if match else None


def filename_match_has_rating_data(match: FilenameMatch) -> bool:
    return match.metacritic is not None or match.user_score is not None


def filename_match_prefer(candidate: FilenameMatch, existing: FilenameMatch | None) -> bool:
    if existing is None:
        return True
    if candidate.status == "ok" and existing.status != "ok":
        return True
    if filename_match_has_rating_data(candidate) and not filename_match_has_rating_data(existing):
        return True
    return False


def legacy_filename_match_alias_keys(key: str) -> list[str]:
    aliases = []
    stripped = key
    while True:
        suffix = next((suffix for suffix in LEGACY_FILENAME_KEY_FORMAT_SUFFIXES if stripped.endswith(suffix)), None)
        if suffix is None:
            return aliases
        stripped = stripped[: -len(suffix)]
        if stripped:
            aliases.append(stripped)


def fill_rating_from_filename_match(rating: Rating, match: FilenameMatch, overwrite: bool = False) -> None:
    if overwrite or rating.metacritic is None:
        rating.metacritic = match.metacritic
    if overwrite or rating.metacritic_url is None:
        rating.metacritic_url = match.metacritic_url
    if overwrite or rating.metacritic_review_count is None:
        rating.metacritic_review_count = match.review_count
    if overwrite or rating.metacritic_user is None:
        rating.metacritic_user = match.user_score
    if overwrite or rating.metacritic_user_url is None:
        rating.metacritic_user_url = match.user_score_url
    if overwrite or rating.metacritic_user_count is None:
        rating.metacritic_user_count = match.user_rating_count
    if match.product_page_parsed:
        rating.metacritic_product_page_parsed = True


def fill_rating_from_metacritic_result(
    rating: Rating,
    result: tuple[Any, ...],
    mark_product_page_parsed: bool = True,
) -> str:
    if len(result) == 3:
        metacritic, metacritic_url, status = result
        review_count = user_score = user_score_url = user_count = None
    elif len(result) == 5:
        metacritic, metacritic_url, user_score, user_score_url, status = result
        review_count = user_count = None
    else:
        metacritic, metacritic_url, review_count, user_score, user_score_url, user_count, status = result

    metacritic, review_count = sanitize_metacritic_score(metacritic, review_count)
    if metacritic is None:
        metacritic_url = None

    if metacritic is not None:
        rating.metacritic = metacritic
    if metacritic_url and (metacritic is not None or rating.metacritic_url is None):
        rating.metacritic_url = metacritic_url
    if review_count is not None:
        rating.metacritic_review_count = review_count
    if status == "ok" or user_score is not None or user_score_url is not None or user_count is not None:
        rating.metacritic_user = user_score
        rating.metacritic_user_url = user_score_url if user_score is not None else None
        rating.metacritic_user_count = user_count if user_score is not None else None
    if mark_product_page_parsed and status in METACRITIC_PRODUCT_PAGE_ATTEMPT_STATUSES:
        rating.metacritic_product_page_parsed = True
    return str(status)


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
    filename_matches: dict[str, FilenameMatch] | None = None,
    advanced_metacritic_lookup: bool = True,
) -> Rating:
    entry_start = time.perf_counter()
    title_start = time.perf_counter()
    title = clean_title(entry)
    rating = Rating(title=title, path=str(entry.resolve()), kind=kind, platform=platform)
    add_debug_timing(rating, "title clean", time.perf_counter() - title_start)
    context = f"{kind}" + (f"/{platform}" if platform else "")
    status_parts = []
    if "metacritic" in sources:
        using_platform_catalog = kind == "game" and platform_ratings is not None
        if using_platform_catalog:
            log(f"[{index}/{total}] Matching {title} against platform catalog ({context})...")
        else:
            log(f"[{index}/{total}] Looking up {title} ({context})...")
        platform_match = None
        match_type = None
        stored_match = None
        cached_match = None
        rejected_slugs: set[str] = set()
        if using_platform_catalog:
            catalog_match_start = time.perf_counter()
            ratings_by_slug = {item.slug: item for item in platform_ratings}
            stored_match = (filename_matches or {}).get(filename_match_key(title))
            if stored_match is not None:
                rejected_slugs = set(stored_match.rejected_slugs)
                if stored_match.status == "ok":
                    if stored_match.slug and stored_match.slug in ratings_by_slug:
                        platform_match = ratings_by_slug[stored_match.slug]
                        match_type = stored_match.match_type if stored_match.match_type == "manual" else "filename"
                    elif filename_match_has_rating_data(stored_match):
                        cached_match = stored_match
                        match_type = "filename-cache"
                elif stored_match.status == "not found":
                    if not advanced_metacritic_lookup or stored_match.lookup_version >= FILENAME_MATCH_LOOKUP_VERSION:
                        status_parts.append("not found")
                    elif DEBUG:
                        debug_log(f"[debug] stale cached miss for {title!r}; retrying advanced Metacritic lookup")
            if platform_match is None and cached_match is None and not status_parts:
                matched = auto_platform_rating_match(title, platform_ratings, rejected_slugs)
                if matched is not None:
                    platform_match, match_type, candidate = matched
                    if DEBUG and match_type == "fuzzy":
                        debug_log(
                            f"[debug] catalog auto-match {title!r} -> {platform_match.title!r} "
                            f"score={candidate.score:.3f} reason={candidate.reason}"
                        )
                else:
                    review_candidates = review_platform_match_candidates(title, platform_ratings, rejected_slugs)
                    for candidate in review_candidates:
                        add_match_review_candidate(rating, candidate)
                    if DEBUG and review_candidates:
                        debug_log(
                            f"[debug] catalog review-candidate {title!r}: "
                            + "; ".join(
                                f"{candidate.rating.title!r} score={candidate.score:.3f} reason={candidate.reason}"
                                for candidate in review_candidates
                            )
                        )
            add_debug_timing(rating, "mc catalog/cache match", time.perf_counter() - catalog_match_start)
        if platform_match is not None:
            rating.metacritic = platform_match.metacritic
            rating.metacritic_url = platform_match.metacritic_url
            rating.metacritic_review_count = platform_match.review_count
            rating.metacritic_user = platform_match.user_score
            rating.metacritic_user_url = platform_match.user_score_url
            rating.metacritic_user_count = platform_match.user_rating_count
            rating.metacritic_product_page_parsed = platform_match.product_page_parsed
            if stored_match is not None and filename_match_has_rating_data(stored_match):
                fill_rating_from_filename_match(rating, stored_match)
            if advanced_metacritic_lookup and kind == "game" and platform and not rating.metacritic_product_page_parsed:
                try:
                    mark_uncached_lookup(rating)
                    product_timings: list[tuple[str, float]] = []
                    product_start = time.perf_counter()
                    metacritic_result = (
                        get_metacritic_slug(platform_match.slug, kind, platform, timeout, product_timings)
                        if DEBUG
                        else get_metacritic_slug(platform_match.slug, kind, platform, timeout)
                    )
                    copy_debug_timings(rating, product_timings)
                    add_debug_timing(rating, "mc product total", time.perf_counter() - product_start)
                    with debug_timing(rating, "mc fill"):
                        product_status = fill_rating_from_metacritic_result(rating, metacritic_result)
                    if product_status != "ok":
                        log(f"  Metacritic product page enrichment skipped: {product_status}")
                except Exception as exc:  # noqa: BLE001 - keep the catalog match if enrichment fails.
                    log(f"  Metacritic product page enrichment failed: {exc}")
            log(f"  Metacritic platform match: {platform_match.title} ({platform_match.metacritic}, {match_type})")
        elif cached_match is not None:
            fill_rating_from_filename_match(rating, cached_match, overwrite=True)
            if advanced_metacritic_lookup and kind == "game" and platform and cached_match.slug and not rating.metacritic_product_page_parsed:
                try:
                    mark_uncached_lookup(rating)
                    product_timings = []
                    product_start = time.perf_counter()
                    metacritic_result = (
                        get_metacritic_slug(cached_match.slug, kind, platform, timeout, product_timings)
                        if DEBUG
                        else get_metacritic_slug(cached_match.slug, kind, platform, timeout)
                    )
                    copy_debug_timings(rating, product_timings)
                    add_debug_timing(rating, "mc product total", time.perf_counter() - product_start)
                    with debug_timing(rating, "mc fill"):
                        product_status = fill_rating_from_metacritic_result(rating, metacritic_result)
                    if product_status != "ok":
                        log(f"  Metacritic cached match enrichment skipped: {product_status}")
                except Exception as exc:  # noqa: BLE001 - keep the cached match if enrichment fails.
                    log(f"  Metacritic cached match enrichment failed: {exc}")
            log(f"  Metacritic cached filename match: {cached_match.title} ({cached_match.slug or 'no slug'})")
        elif status_parts:
            pass
        elif using_platform_catalog and not advanced_metacritic_lookup:
            log("  Metacritic platform catalog miss")
            status_parts.append("not found")
        else:
            try:
                mark_uncached_lookup(rating)
                lookup_timings: list[tuple[str, float]] = []
                lookup_start = time.perf_counter()
                if DEBUG:
                    metacritic_result = get_metacritic(title, kind, platform, timeout, lookup_timings, rejected_slugs)
                elif rejected_slugs:
                    metacritic_result = get_metacritic(title, kind, platform, timeout, excluded_slugs=rejected_slugs)
                else:
                    metacritic_result = get_metacritic(title, kind, platform, timeout)
                copy_debug_timings(rating, lookup_timings)
                add_debug_timing(rating, "mc lookup total", time.perf_counter() - lookup_start)
                with debug_timing(rating, "mc fill"):
                    metacritic_status = fill_rating_from_metacritic_result(rating, metacritic_result)
                if metacritic_status != "ok":
                    status_parts.append(metacritic_status)
            except Exception as exc:  # noqa: BLE001 - preserve lookup failure in cache.
                status_parts.append(f"metacritic error: {exc}")

    if kind == "game" and "opencritic" in sources:
        try:
            mark_uncached_lookup(rating)
            with debug_timing(rating, "opencritic total"):
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
        f"MC User={format_metacritic_user_score(rating.metacritic_user, '-')} "
        f"OC={rating.opencritic if rating.opencritic is not None else '-'} "
        f"combined={rating.combined if rating.combined is not None else '-'} "
        f"status={rating.status}",
    )
    add_debug_timing(rating, "entry total", time.perf_counter() - entry_start)
    debug_log(
        f"[debug] [{index}/{total}] {entry.name} "
        f"network={'yes' if used_uncached_lookup(rating) else 'no'} "
        f"status={rating.status} "
        f"{format_debug_timing_summary(rating)}"
    )
    return rating


def refresh_ratings(
    entries: list[Path],
    kind: str,
    platform: str | None,
    sources: set[str],
    delay: float,
    timeout: float,
    workers: int,
    platform_ratings: list[PlatformRating] | None = None,
    filename_matches: dict[str, FilenameMatch] | None = None,
    advanced_metacritic_lookup: bool = True,
) -> list[Rating]:
    pending = list(enumerate(entries, start=1))
    worker_count = max(1, workers)
    may_use_uncached_lookup = delay and (
        "opencritic" in sources
        or platform_ratings is None
        or advanced_metacritic_lookup
        or kind != "game"
    )
    progress = create_progress()
    rating_task = None
    if progress is not None:
        if kind == "game" and platform_ratings is not None and advanced_metacritic_lookup:
            description = "Advanced Metacritic entries"
        elif kind == "game" and platform_ratings is not None:
            description = "Metacritic catalog matching"
        else:
            description = "Rating entries"
        progress.start()
        rating_task = progress.add_task(description, total=len(pending), detail="")
    rows = []
    try:
        if worker_count == 1:
            for index, entry in pending:
                if rating_task is not None:
                    progress.update(rating_task, detail=progress_detail(entry.name))
                rating = lookup_rating(
                    index,
                    len(entries),
                    entry,
                    kind,
                    platform,
                    sources,
                    timeout,
                    platform_ratings,
                    filename_matches,
                    advanced_metacritic_lookup,
                )
                rows.append(rating)
                if rating_task is not None:
                    detail = progress_detail(entry.name)
                    if used_uncached_lookup(rating):
                        detail = f"{detail} (network)"
                    progress.update(rating_task, advance=1, detail=detail)
                if delay and used_uncached_lookup(rating):
                    time.sleep(delay)
            return rows

        if may_use_uncached_lookup:
            log("--delay is ignored when --workers is greater than 1.")
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
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
                    filename_matches,
                    advanced_metacritic_lookup,
                ): entry
                for index, entry in pending
            }
            for future in concurrent.futures.as_completed(futures):
                entry = futures[future]
                rating = future.result()
                rows.append(rating)
                if rating_task is not None:
                    detail = progress_detail(entry.name)
                    if used_uncached_lookup(rating):
                        detail = f"{detail} (network)"
                    progress.update(rating_task, advance=1, detail=detail)
        return sorted(rows, key=lambda row: row.path.lower())
    finally:
        if progress is not None:
            progress.stop()


def sorted_ratings(rows: list[Rating], entries: list[Path] | None = None) -> list[Rating]:
    if entries is not None:
        keys = {str(entry.resolve()) for entry in entries}
        rows = [rating for rating in rows if rating.path in keys]
    rows = [rating for rating in rows if is_rankable(rating)]
    return sorted(
        rows,
        key=lambda row: (
            row.combined is None,
            -(row.combined or -1),
            row.title.lower(),
        ),
    )


def update_filename_matches_from_ratings(
    matches: dict[str, FilenameMatch],
    ratings: list[Rating],
    platform_ratings: list[PlatformRating] | None,
) -> bool:
    if platform_ratings is None:
        return False
    platform_slugs = {rating.slug for rating in platform_ratings}
    changed = False
    for rating in ratings:
        key = filename_match_key(rating.title)
        slug = platform_rating_slug_from_url(rating.metacritic_url) or platform_rating_slug_from_url(rating.metacritic_user_url)
        if slug and (rating.status == "ok" or rating.metacritic is not None or rating.metacritic_user is not None):
            old_match = matches.get(key)
            match_type = "matched" if slug in platform_slugs else "lookup"
            rejected_slugs = old_match.rejected_slugs if old_match is not None else []
            if old_match is not None and old_match.match_type == "manual" and old_match.slug == slug:
                match_type = "manual"
            new_match = FilenameMatch(
                title=rating.title,
                slug=slug,
                status="ok",
                match_type=match_type,
                metacritic=rating.metacritic,
                metacritic_url=rating.metacritic_url,
                review_count=rating.metacritic_review_count,
                user_score=rating.metacritic_user,
                user_score_url=rating.metacritic_user_url,
                user_rating_count=rating.metacritic_user_count,
                product_page_parsed=rating.metacritic_product_page_parsed,
                rejected_slugs=[rejected_slug for rejected_slug in rejected_slugs if rejected_slug != slug],
            )
        elif rating.status in METACRITIC_CACHEABLE_MISS_STATUSES:
            old_match = matches.get(key)
            status = "reviewed" if old_match is not None and old_match.status == "reviewed" and old_match.rejected_slugs else "not found"
            match_type = old_match.match_type if status == "reviewed" and old_match is not None else "miss"
            new_match = FilenameMatch(
                title=rating.title,
                slug=None,
                status=status,
                match_type=match_type,
                rejected_slugs=old_match.rejected_slugs if old_match is not None else [],
            )
        else:
            continue
        old_match = matches.get(key)
        if (
            old_match is None
            or old_match.slug != new_match.slug
            or old_match.status != new_match.status
            or old_match.title != new_match.title
            or old_match.match_type != new_match.match_type
            or old_match.metacritic != new_match.metacritic
            or old_match.metacritic_url != new_match.metacritic_url
            or old_match.review_count != new_match.review_count
            or old_match.user_score != new_match.user_score
            or old_match.user_score_url != new_match.user_score_url
            or old_match.user_rating_count != new_match.user_rating_count
            or old_match.product_page_parsed != new_match.product_page_parsed
            or old_match.rejected_slugs != new_match.rejected_slugs
            or old_match.lookup_version != new_match.lookup_version
        ):
            matches[key] = new_match
            changed = True
    return changed


def update_platform_ratings_from_rows(platform_ratings: list[PlatformRating] | None, ratings: list[Rating]) -> bool:
    if platform_ratings is None:
        return False
    by_slug = {rating.slug: rating for rating in platform_ratings}
    changed = False
    now = time.time()
    for row in ratings:
        slug = platform_rating_slug_from_url(row.metacritic_url)
        if not slug:
            continue
        platform_rating = by_slug.get(slug)
        if platform_rating is None:
            continue

        row_changed = False
        if row.metacritic is not None and platform_rating.metacritic != row.metacritic:
            platform_rating.metacritic = row.metacritic
            row_changed = True
        if row.metacritic_url and platform_rating.metacritic_url != row.metacritic_url:
            platform_rating.metacritic_url = row.metacritic_url
            row_changed = True
        if row.metacritic_review_count is not None and platform_rating.review_count != row.metacritic_review_count:
            platform_rating.review_count = row.metacritic_review_count
            row_changed = True

        if row.metacritic_product_page_parsed:
            if not platform_rating.product_page_parsed:
                platform_rating.product_page_parsed = True
                row_changed = True
            if platform_rating.user_score != row.metacritic_user:
                platform_rating.user_score = row.metacritic_user
                row_changed = True
            if platform_rating.user_score_url != row.metacritic_user_url:
                platform_rating.user_score_url = row.metacritic_user_url
                row_changed = True
            if platform_rating.user_rating_count != row.metacritic_user_count:
                platform_rating.user_rating_count = row.metacritic_user_count
                row_changed = True
            new_user_updated_at = now if row.metacritic_user is not None else None
            if platform_rating.user_score_updated_at != new_user_updated_at:
                platform_rating.user_score_updated_at = new_user_updated_at
                row_changed = True

        if row_changed:
            platform_rating.updated_at = now
            changed = True
    return changed


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


def emit_paths(rows: list[Rating]) -> None:
    for row in rows:
        print(row.path)


def format_score_with_count(score: str, count: int | None) -> str:
    if count is None:
        return score
    return f"{score} ({count:,})"


def format_metacritic_score(score: int | None, count: int | None = None, missing: str = "") -> str:
    if score is None:
        return missing
    return format_score_with_count(str(score), count)


def format_metacritic_user_score(score: float | None, missing: str = "", count: int | None = None) -> str:
    if score is None:
        return missing
    return format_score_with_count(f"{score:.1f}", count)


def resolve_output_format(output_format: str) -> str:
    if output_format == "auto":
        return "table" if sys.stdout.isatty() else "paths"
    return output_format


def print_rankings(rows: list[Rating], limit: int | None = None, output_format: str = "table") -> None:
    shown = rows[:limit] if limit else rows
    output_format = resolve_output_format(output_format)
    if output_format == "paths":
        emit_paths(shown)
        return
    if output_format == "json":
        print(json.dumps([dataclasses.asdict(row) for row in shown], indent=2, sort_keys=True))
        return
    if output_format == "tsv":
        print("rank\tscore\tmetacritic\tmc_user\topencritic\tkind\ttitle\tpath")
        for idx, row in enumerate(shown, start=1):
            score = f"{row.combined:.1f}" if row.combined is not None else ""
            mc = format_metacritic_score(row.metacritic, row.metacritic_review_count)
            mc_user = format_metacritic_user_score(row.metacritic_user, count=row.metacritic_user_count)
            oc = str(row.opencritic) if row.opencritic is not None else ""
            print(f"{idx}\t{score}\t{mc}\t{mc_user}\t{oc}\t{row.kind}\t{row.title}\t{row.path}")
        return

    print("\nRank  Score  MC          MC User    OC   Type   Title")
    print("----  -----  ----------  ---------  ---  -----  -----")
    for idx, row in enumerate(shown, start=1):
        score = f"{row.combined:.1f}" if row.combined is not None else "N/A"
        mc = format_metacritic_score(row.metacritic, row.metacritic_review_count, "-")
        mc_user = format_metacritic_user_score(row.metacritic_user, "-", row.metacritic_user_count)
        oc = str(row.opencritic) if row.opencritic is not None else "-"
        print(f"{idx:>4}  {score:>5}  {mc:>10}  {mc_user:>9}  {oc:>3}  {row.kind:<5}  {row.title}")


def bottom_rows(rows: list[Rating], count: int) -> list[Rating]:
    rated = [row for row in rows if row.combined is not None]
    return list(reversed(rated[-count:]))


def delete_rows(rows: list[Rating], yes: bool) -> None:
    for row in rows:
        path = Path(row.path)
        log(f"{'Deleting' if yes else 'Would delete'}: {path}")
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
        log(f"{'Moving' if yes else 'Would move'}: {source} -> {target}")
        if yes:
            if target.exists():
                raise FileExistsError(f"Destination already exists: {target}")
            shutil.move(str(source), str(target))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rank games, movies, or TV folders using Metacritic/OpenCritic, with a system-wide JSON cache.",
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
    parser.add_argument("--limit", type=int, help="Limit rows shown for rank output.")
    parser.add_argument(
        "--order",
        choices=("best", "worst"),
        default="best",
        help="Sort rank output best-first or worst-first. Use --order worst for pipe-friendly tail cleanup.",
    )
    parser.add_argument(
        "--format",
        choices=("auto", "table", "paths", "json", "tsv"),
        default="auto",
        help="Output format for rank action. Auto prints a table in a terminal and paths when piped.",
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
        "--advanced",
        action="store_true",
        help=(
            "Use advanced Metacritic product-page lookups: enrich catalog matches with user scores/counts "
            "and search catalog misses."
        ),
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
    parser.add_argument(
        "--write-match-review",
        type=Path,
        help="Write borderline Metacritic catalog match candidates to this TSV file.",
    )
    parser.add_argument(
        "--apply-match-review",
        type=Path,
        help="Apply accepted/rejected decisions from a match review TSV before ranking.",
    )
    parser.add_argument("--verbose", action="store_true", help="Show detailed lookup URLs and action status on stderr.")
    parser.add_argument("--debug", action="store_true", help="Show per-entry fetch/parse timing diagnostics on stderr.")
    return parser.parse_args()


def main() -> int:
    global VERBOSE, DEBUG
    args = parse_args()
    VERBOSE = args.verbose
    DEBUG = args.debug
    if args.workers < 1:
        warn("--workers must be at least 1.")
        return 2
    if args.score_range is not None:
        try:
            parse_score_range(args.score_range)
        except ValueError as exc:
            warn(str(exc))
            return 2

    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        warn(f"Folder does not exist: {folder}")
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
        warn("No matching entries found.")
        return 1

    platform = args.platform or platform_from_context(folder)
    kind = args.kind or kind_from_context(folder, platform)
    sources = {"metacritic", "opencritic"} if args.sources == "both" else {args.sources}
    advanced_lookup = args.advanced
    log(
        f"Detected context: kind={kind}" + (f", platform={platform}" if platform else "")
        + f", sources={','.join(sorted(sources))}",
    )
    platform_cache_path = args.platform_cache.expanduser().resolve() if args.platform_cache else system_cache_path()
    platform_ratings = None
    filename_matches: dict[str, FilenameMatch] = {}
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
        filename_matches = load_filename_matches(platform_cache_path, platform)
        if args.apply_match_review:
            if apply_match_review(args.apply_match_review, filename_matches, platform_ratings):
                save_filename_matches(platform_cache_path, platform, filename_matches)
                log(f"Applied match review decisions from {args.apply_match_review}.")
    elif args.apply_match_review:
        warn("--apply-match-review requires a game platform catalog.")
        return 2
    looked_up_rows = refresh_ratings(
        entries,
        kind,
        platform,
        sources,
        args.delay,
        args.timeout,
        args.workers,
        platform_ratings,
        filename_matches,
        advanced_lookup or platform_ratings is None,
    )
    if platform and update_filename_matches_from_ratings(filename_matches, looked_up_rows, platform_ratings):
        save_filename_matches(platform_cache_path, platform, filename_matches)
    if platform and update_platform_ratings_from_rows(platform_ratings, looked_up_rows):
        save_platform_ratings(
            platform_cache_path,
            platform,
            platform_ratings or [],
            created_at=platform_cache_created_at(platform_cache_path, platform),
            updated_at=time.time(),
        )
    if args.write_match_review:
        review_candidates = collect_match_review_candidates(looked_up_rows)
        write_match_review(args.write_match_review, review_candidates)
        log(f"Wrote {len(review_candidates)} match review candidates to {args.write_match_review}.")
    rows = sorted_ratings(looked_up_rows, entries)
    action_rows = filter_rows_by_score_range(rows, args.score_range)
    ordered_rows = filter_rows_by_score_range(order_rows(rows, args.order), args.score_range)

    if args.action == "delete-bottom":
        selected = bottom_rows(action_rows, args.count)
        log(f"Selected bottom {len(selected)} rated entries.")
        emit_paths(selected)
        delete_rows(selected, args.yes)
    elif args.action == "move-top":
        if not args.dest:
            warn("--dest is required for move-top.")
            return 2
        selected = [row for row in action_rows if row.combined is not None][: args.count]
        log(f"Selected top {len(selected)} rated entries.")
        emit_paths(selected)
        move_rows(selected, args.dest.expanduser().resolve(), args.yes)
    else:
        print_rankings(ordered_rows, args.limit, args.format)

    if args.action != "rank" and not args.yes:
        log("Dry-run only. Add --yes to perform the action.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
