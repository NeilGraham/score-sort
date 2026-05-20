"""Command-line interface for score-sort."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from . import core


DEFAULT_DELAY_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_SOURCES = {"metacritic"}


def default_sort_direction(sort_key: str) -> str:
    if sort_key == "score":
        return "desc"
    return "asc"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rank games, movies, or TV folders with Metacritic scores.",
    )
    parser.add_argument("folder", type=Path, help="Folder containing files or first-level folders to rank.")
    parser.add_argument("--refresh", action="store_true", help="Refresh cached platform catalog data before ranking.")
    parser.add_argument(
        "--sort",
        choices=("score", "name"),
        default="score",
        help="Sort rows by score or cleaned name.",
    )
    parser.add_argument(
        "--direction",
        choices=("asc", "desc"),
        help="Sort direction. Defaults to desc for score and asc for name.",
    )
    parser.add_argument(
        "--range",
        dest="score_range",
        help="Only include scored entries in a range, such as 1-50, >70, >=70, <40, or <=40.",
    )
    parser.add_argument(
        "--kind",
        choices=("game", "movie", "tv"),
        help="Override context detection. Defaults to inferred from the folder path.",
    )
    parser.add_argument("--platform", help="Metacritic platform slug, such as nintendo-ds, 3ds, ps2, or xbox-360.")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of parallel lookup workers.",
    )
    parser.add_argument(
        "--advanced",
        action="store_true",
        help="Use Metacritic product-page lookups to enrich catalog matches and search catalog misses.",
    )
    return parser.parse_args(argv)


def parse_extensions(raw_extensions: str) -> set[str]:
    return {
        ext.strip().lower() if ext.strip().startswith(".") else f".{ext.strip().lower()}"
        for ext in raw_extensions.split(",")
        if ext.strip()
    }


def main() -> int:
    args = parse_args()
    core.VERBOSE = False
    core.DEBUG = False

    if args.workers < 1:
        core.warn("--workers must be at least 1.")
        return 2
    if args.score_range is not None:
        try:
            core.parse_score_range(args.score_range)
        except ValueError as exc:
            core.warn(str(exc))
            return 2

    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        core.warn(f"Folder does not exist: {folder}")
        return 2

    entries = core.iter_games(folder, parse_extensions(core.DEFAULT_EXTENSIONS), include_dirs=True)
    if not entries:
        core.warn("No matching entries found.")
        return 1

    platform = args.platform or core.platform_from_context(folder)
    kind = args.kind or core.kind_from_context(folder, platform)
    platform_cache_path = core.system_cache_path()
    platform_ratings = None
    filename_matches: dict[str, core.FilenameMatch] = {}

    if kind == "game" and platform:
        platform_ratings = core.ensure_platform_ratings(
            platform_cache_path,
            platform,
            args.refresh,
            DEFAULT_TIMEOUT_SECONDS,
            DEFAULT_DELAY_SECONDS,
            args.workers,
            max_pages=None,
            max_age_days=core.DEFAULT_PLATFORM_CACHE_DAYS,
        )
        filename_matches = core.load_filename_matches(platform_cache_path, platform)

    looked_up_rows = core.refresh_ratings(
        entries,
        kind,
        platform,
        DEFAULT_SOURCES,
        DEFAULT_DELAY_SECONDS,
        DEFAULT_TIMEOUT_SECONDS,
        args.workers,
        platform_ratings,
        filename_matches,
        args.advanced or platform_ratings is None,
    )
    if platform and core.update_filename_matches_from_ratings(filename_matches, looked_up_rows, platform_ratings):
        core.save_filename_matches(platform_cache_path, platform, filename_matches)
    if platform and core.update_platform_ratings_from_rows(platform_ratings, looked_up_rows):
        core.save_platform_ratings(
            platform_cache_path,
            platform,
            platform_ratings or [],
            created_at=core.platform_cache_created_at(platform_cache_path, platform),
            updated_at=time.time(),
        )

    direction = args.direction or default_sort_direction(args.sort)
    rows = core.sorted_ratings(looked_up_rows, entries)
    rows = core.filter_rows_by_score_range(rows, args.score_range)
    rows = core.sort_rows(rows, args.sort, direction)
    core.print_rankings(rows, output_format="table")
    return 0
