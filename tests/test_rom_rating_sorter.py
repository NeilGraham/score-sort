import dataclasses
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import rom_rating_sorter as sorter


class RatingSorterTests(unittest.TestCase):
    def test_clean_title_removes_common_release_noise(self) -> None:
        title = sorter.clean_title(Path("Chrono_Trigger_(USA)_[Rev 1].nds"))

        self.assertEqual(title, "Chrono Trigger")

    def test_slug_candidates_include_rom_catalog_variants(self) -> None:
        self.assertIn("the-bigs-2", sorter.slug_candidates("Bigs 2, The"))
        self.assertIn("cop-the-recruit", sorter.slug_candidates("C O P - The Recruit"))
        self.assertIn("syberia", sorter.slug_candidates("B Sokal Syberia"))

    def test_sorted_ratings_puts_high_scores_first_and_unrated_last(self) -> None:
        entries = [Path("low.nds"), Path("high.nds"), Path("missing.nds")]
        cache = {}
        for entry, score in zip(entries, [72, 95, None], strict=True):
            rating = sorter.Rating(title=entry.stem, path=str(entry.resolve()), metacritic=score)
            rating.calculate()
            if rating.combined is None:
                rating.status = "not found"
            cache[str(entry.resolve())] = rating

        rows = sorter.sorted_ratings(cache, entries)

        self.assertEqual([row.title for row in rows], ["high", "low", "missing"])

    def test_sorted_ratings_excludes_lookup_failures(self) -> None:
        entries = [Path("scored.nds"), Path("unscored.nds"), Path("missing-page.nds"), Path("timeout.nds")]
        cache = {}
        rows = [
            sorter.Rating(title="scored", path=str(entries[0].resolve()), metacritic=80, combined=80),
            sorter.Rating(title="unscored", path=str(entries[1].resolve()), status="not found"),
            sorter.Rating(title="missing page", path=str(entries[2].resolve()), status="metacritic 404"),
            sorter.Rating(title="timeout", path=str(entries[3].resolve()), status="metacritic error: timed out"),
        ]
        for row in rows:
            cache[row.path] = row

        sorted_rows = sorter.sorted_ratings(cache, entries)

        self.assertEqual([row.title for row in sorted_rows], ["scored", "unscored"])

    def test_order_rows_can_return_worst_rated_first(self) -> None:
        rows = [
            sorter.Rating(title="best", path="best", combined=95),
            sorter.Rating(title="mid", path="mid", combined=75),
            sorter.Rating(title="unknown", path="unknown", combined=None),
        ]

        ordered = sorter.order_rows(rows, "worst")

        self.assertEqual([row.title for row in ordered], ["mid", "best", "unknown"])

    def test_filter_rows_by_score_range_supports_inclusive_span(self) -> None:
        rows = [
            sorter.Rating(title="low", path="low", combined=49),
            sorter.Rating(title="edge", path="edge", combined=50),
            sorter.Rating(title="high", path="high", combined=51),
            sorter.Rating(title="unknown", path="unknown", combined=None),
        ]

        filtered = sorter.filter_rows_by_score_range(rows, "1-50")

        self.assertEqual([row.title for row in filtered], ["low", "edge"])

    def test_filter_rows_by_score_range_supports_comparisons(self) -> None:
        rows = [
            sorter.Rating(title="forty", path="forty", combined=40),
            sorter.Rating(title="seventy", path="seventy", combined=70),
            sorter.Rating(title="great", path="great", combined=71),
        ]

        self.assertEqual([row.title for row in sorter.filter_rows_by_score_range(rows, ">70")], ["great"])
        self.assertEqual([row.title for row in sorter.filter_rows_by_score_range(rows, "<=40")], ["forty"])

    def test_parse_score_range_rejects_invalid_expressions(self) -> None:
        with self.assertRaises(ValueError):
            sorter.parse_score_range("70..90")
        with self.assertRaises(ValueError):
            sorter.parse_score_range("90-70")
        with self.assertRaises(ValueError):
            sorter.parse_score_range(">101")

    def test_action_selection_can_use_score_range_pool(self) -> None:
        rows = [
            sorter.Rating(title="best", path="best", combined=95),
            sorter.Rating(title="mid", path="mid", combined=75),
            sorter.Rating(title="low", path="low", combined=40),
            sorter.Rating(title="lower", path="lower", combined=30),
        ]

        selected = sorter.bottom_rows(sorter.filter_rows_by_score_range(rows, "1-50"), 1)

        self.assertEqual([row.title for row in selected], ["lower"])

    def test_paths_format_is_pipe_friendly(self) -> None:
        rows = [
            sorter.Rating(title="A", path="C:/media/A.nds", combined=90),
            sorter.Rating(title="B", path="C:/media/B.nds", combined=80),
        ]
        output = io.StringIO()

        with redirect_stdout(output):
            sorter.print_rankings(rows, limit=1, output_format="paths")

        self.assertEqual(output.getvalue(), "C:/media/A.nds\n")

    def test_json_format_outputs_rating_objects(self) -> None:
        row = sorter.Rating(title="A", path="C:/media/A.nds", combined=90)
        output = io.StringIO()

        with redirect_stdout(output):
            sorter.print_rankings([row], output_format="json")

        self.assertEqual(json.loads(output.getvalue()), [dataclasses.asdict(row)])

    def test_parse_metacritic_score_ignores_unscored_visible_text(self) -> None:
        page = (
            "<html><body><style>.score{font-size:.9rem}</style>"
            "<div>Metascore Available after 4 critic reviews</div></body></html>"
        )

        self.assertIsNone(sorter.parse_metacritic_score(page))

    def test_parse_metacritic_score_from_nuxt_reference_payload(self) -> None:
        payload = [
            {"type": 1, "title": 2, "slug": 3, "criticScoreSummary": 4},
            "game-title",
            "Ace Attorney Investigations: Miles Edgeworth",
            "ace-attorney-investigations-miles-edgeworth",
            {"score": 5, "reviewCount": 6},
            78,
            51,
        ]
        page = (
            '<script type="application/json" data-nuxt-data="nuxt-app" '
            f'id="__NUXT_DATA__">{json.dumps(payload)}</script>'
        )

        self.assertEqual(sorter.parse_metacritic_score(page), 78)
        self.assertEqual(sorter.parse_metacritic_score(page, "ace-attorney-investigations-miles-edgeworth"), 78)
        self.assertIsNone(sorter.parse_metacritic_score(page, "different-game"))

    def test_parse_metacritic_score_does_not_use_related_title_score(self) -> None:
        payload = [
            {"type": 1, "title": 2, "slug": 3, "criticScoreSummary": 4},
            "game-title",
            "100 All-Time Favorites",
            "100-all-time-favorites",
            {"score": 5},
            None,
            {"type": 1, "title": 7, "slug": 8, "criticScoreSummary": 9},
            "Art of Balance",
            "art-of-balance-touch",
            {"score": 10},
            88,
        ]
        page = (
            '<script type="application/json" data-nuxt-data="nuxt-app" '
            f'id="__NUXT_DATA__">{json.dumps(payload)}</script>'
        )

        self.assertIsNone(sorter.parse_metacritic_score(page, "100-all-time-favorites"))

    def test_parse_metacritic_search_slugs_ranks_matching_game_titles(self) -> None:
        payload = [
            {"type": 1, "title": 2, "slug": 3},
            "game-title",
            "Syberia: The World Before",
            "syberia-the-world-before",
            {"type": 1, "title": 5, "slug": 6},
            "Syberia",
            "syberia",
            {"type": 7, "title": 8, "slug": 9},
            "person",
            "Benoit Sokal",
        ]
        page = (
            '<script type="application/json" data-nuxt-data="nuxt-app" '
            f'id="__NUXT_DATA__">{json.dumps(payload)}</script>'
        )

        self.assertEqual(sorter.parse_metacritic_search_slugs(page, "Syberia", limit=1), ["syberia"])

    def test_parse_metacritic_browse_entries_reads_platform_results(self) -> None:
        payload = [
            {"type": 1, "title": 2, "slug": 3, "criticScoreSummary": 4},
            "game-title",
            "Chrono Trigger",
            "chrono-trigger",
            {"score": 5},
            92,
            {"type": 6, "title": 7, "slug": 8, "criticScoreSummary": 9},
            "movie-title",
            "Chrono Trigger: The Movie",
            "chrono-trigger-the-movie",
            {"score": 10},
            70,
        ]
        page = (
            '<script type="application/json" data-nuxt-data="nuxt-app" '
            f'id="__NUXT_DATA__">{json.dumps(payload)}</script>'
        )

        entries = sorter.parse_metacritic_browse_entries(page)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "Chrono Trigger")
        self.assertEqual(entries[0].metacritic, 92)
        self.assertEqual(entries[0].metacritic_url, "https://www.metacritic.com/game/chrono-trigger/")

    def test_parse_metacritic_total_pages_uses_last_pagination_number(self) -> None:
        page = (
            '<span class="c-navigation-pagination__item-content" data-v-x>1</span>'
            '<span class="c-navigation-pagination__item-content" data-v-x>2</span>'
            '<span class="c-navigation-pagination__ellipsis" data-v-x>...</span>'
            '<span class="c-navigation-pagination__item-content" data-v-x>31</span>'
        )

        self.assertEqual(sorter.parse_metacritic_total_pages(page), 31)

    def test_fetch_metacritic_platform_ratings_uses_declared_pages(self) -> None:
        pages = []

        def make_page(title: str, slug: str, score: int, total_pages: int | None = None) -> str:
            payload = [
                {"type": 1, "title": 2, "slug": 3, "criticScoreSummary": 4},
                "game-title",
                title,
                slug,
                {"score": 5},
                score,
            ]
            pagination = ""
            if total_pages is not None:
                pagination = (
                    '<span class="c-navigation-pagination__item-content">1</span>'
                    f'<span class="c-navigation-pagination__item-content">{total_pages}</span>'
                )
            return (
                pagination
                + '<script type="application/json" data-nuxt-data="nuxt-app" '
                + f'id="__NUXT_DATA__">{json.dumps(payload)}</script>'
            )

        original = sorter.fetch_text

        def fake_fetch_text(url: str, timeout: float) -> str:
            page_number = int(url.rsplit("page=", 1)[1])
            pages.append(page_number)
            return make_page(f"Game {page_number}", f"game-{page_number}", 90 - page_number, total_pages=3)

        try:
            sorter.fetch_text = fake_fetch_text
            ratings = sorter.fetch_metacritic_platform_ratings(
                "nintendo-ds",
                timeout=1,
                delay=0,
                workers=2,
            )
        finally:
            sorter.fetch_text = original

        self.assertEqual(sorted(pages), [1, 2, 3])
        self.assertEqual([rating.slug for rating in ratings], ["game-1", "game-2", "game-3"])

    def test_system_platform_cache_preserves_user_scores_on_refresh(self) -> None:
        previous = [
            sorter.PlatformRating(
                title="Chrono Trigger",
                slug="chrono-trigger",
                metacritic=91,
                metacritic_url="https://www.metacritic.com/game/chrono-trigger/",
                updated_at=10,
                user_score=9.2,
                user_score_url="https://www.metacritic.com/game/chrono-trigger/user-reviews/",
                user_score_updated_at=20,
            )
        ]
        refreshed = [
            sorter.PlatformRating(
                title="Chrono Trigger",
                slug="chrono-trigger",
                metacritic=92,
                metacritic_url="https://www.metacritic.com/game/chrono-trigger/",
            )
        ]

        merged = sorter.preserve_platform_user_scores(previous, refreshed, created_at=30)

        self.assertEqual(merged[0].metacritic, 92)
        self.assertEqual(merged[0].updated_at, 30)
        self.assertEqual(merged[0].user_score, 9.2)
        self.assertEqual(merged[0].user_score_updated_at, 20)

    def test_save_platform_ratings_uses_system_cache_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "platform-cache.json"
            rating = sorter.PlatformRating(
                title="Chrono Trigger",
                slug="chrono-trigger",
                metacritic=92,
                metacritic_url="https://www.metacritic.com/game/chrono-trigger/",
                updated_at=123,
            )

            sorter.save_platform_ratings(path, "nintendo-ds", [rating], created_at=123)
            payload = json.loads(path.read_text(encoding="utf-8"))

            self.assertIn("platforms", payload)
            self.assertNotIn("items", payload)
            self.assertEqual(payload["platforms"]["nintendo-ds"]["created_at"], 123)
            self.assertEqual(sorter.load_platform_ratings(path, "nintendo-ds")[0].slug, "chrono-trigger")

    def test_best_platform_rating_match_prefers_exact_slug(self) -> None:
        ratings = [
            sorter.PlatformRating(
                title="Chrono Trigger",
                slug="chrono-trigger",
                metacritic=92,
                metacritic_url="https://www.metacritic.com/game/chrono-trigger/",
            )
        ]

        match = sorter.best_platform_rating_match("Chrono Trigger", ratings)

        self.assertIsNotNone(match)
        self.assertEqual(match.metacritic, 92)

    def test_best_platform_rating_match_does_not_drop_sequel_number(self) -> None:
        ratings = [
            sorter.PlatformRating(
                title="Lost in Blue",
                slug="lost-in-blue",
                metacritic=69,
                metacritic_url="https://www.metacritic.com/game/lost-in-blue/",
            )
        ]

        self.assertIsNone(sorter.best_platform_rating_match("Lost in Blue 3", ratings))

    def test_lookup_rating_uses_platform_catalog_before_network_lookup(self) -> None:
        entry = Path("Chrono Trigger (USA).nds")
        ratings = [
            sorter.PlatformRating(
                title="Chrono Trigger",
                slug="chrono-trigger",
                metacritic=92,
                metacritic_url="https://www.metacritic.com/game/chrono-trigger/",
            )
        ]
        original = sorter.get_metacritic

        def unexpected_get_metacritic(title: str, kind: str, platform: str | None, timeout: float):
            raise AssertionError("per-title lookup should not run")

        try:
            sorter.get_metacritic = unexpected_get_metacritic
            rating = sorter.lookup_rating(
                1,
                1,
                entry,
                "game",
                "nintendo-ds",
                {"metacritic"},
                1,
                ratings,
            )
        finally:
            sorter.get_metacritic = original

        self.assertEqual(rating.metacritic, 92)
        self.assertEqual(rating.metacritic_url, "https://www.metacritic.com/game/chrono-trigger/")

    def test_lookup_rating_can_skip_per_title_metacritic_fallback(self) -> None:
        entry = Path("Unknown Game.nds")
        original = sorter.get_metacritic

        def unexpected_get_metacritic(title: str, kind: str, platform: str | None, timeout: float):
            raise AssertionError("per-title lookup should not run")

        try:
            sorter.get_metacritic = unexpected_get_metacritic
            rating = sorter.lookup_rating(
                1,
                1,
                entry,
                "game",
                "nintendo-ds",
                {"metacritic"},
                1,
                [],
                allow_metacritic_fallback=False,
            )
        finally:
            sorter.get_metacritic = original

        self.assertIsNone(rating.metacritic)
        self.assertEqual(rating.status, "not found")

    def test_refresh_ratings_with_workers_updates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            entries = []
            for name in ["Low.nds", "High.nds"]:
                path = folder / name
                path.write_text("", encoding="utf-8")
                entries.append(path)
            cache = {}
            original = sorter.get_metacritic

            def fake_get_metacritic(title: str, kind: str, platform: str | None, timeout: float):
                return (90 if title == "High" else 50), f"https://example.test/{title}", "ok"

            try:
                sorter.get_metacritic = fake_get_metacritic
                sorter.refresh_ratings(
                    folder,
                    entries,
                    cache,
                    "game",
                    "ds",
                    {"metacritic"},
                    refresh=False,
                    refresh_failed=False,
                    delay=0,
                    timeout=1,
                    workers=2,
                )
            finally:
                sorter.get_metacritic = original

            self.assertEqual(sorted(row.metacritic for row in cache.values()), [50, 90])
            self.assertTrue((folder / sorter.CACHE_NAME).exists())

    def test_refresh_ratings_retries_timed_out_cache_entries_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            timeout_entry = folder / "Timeout.nds"
            missing_entry = folder / "Missing.nds"
            ok_entry = folder / "OK.nds"
            entries = [timeout_entry, missing_entry, ok_entry]
            for entry in entries:
                entry.write_text("", encoding="utf-8")

            cache = {
                str(timeout_entry.resolve()): sorter.Rating(
                    title="Timeout",
                    path=str(timeout_entry.resolve()),
                    platform="ds",
                    status="metacritic error: The read operation timed out",
                ),
                str(missing_entry.resolve()): sorter.Rating(
                    title="Missing",
                    path=str(missing_entry.resolve()),
                    platform="ds",
                    status="metacritic 404",
                ),
                str(ok_entry.resolve()): sorter.Rating(
                    title="OK",
                    path=str(ok_entry.resolve()),
                    platform="ds",
                    metacritic=80,
                    combined=80,
                    status="ok",
                ),
            }
            calls = []
            original = sorter.get_metacritic

            def fake_get_metacritic(title: str, kind: str, platform: str | None, timeout: float):
                calls.append(title)
                return 75, f"https://example.test/{title}", "ok"

            try:
                sorter.get_metacritic = fake_get_metacritic
                sorter.refresh_ratings(
                    folder,
                    entries,
                    cache,
                    "game",
                    "ds",
                    {"metacritic"},
                    refresh=False,
                    refresh_failed=False,
                    delay=0,
                    timeout=1,
                    workers=1,
                )
            finally:
                sorter.get_metacritic = original

            self.assertEqual(calls, ["Timeout"])
            self.assertEqual(cache[str(timeout_entry.resolve())].status, "ok")
            self.assertEqual(cache[str(missing_entry.resolve())].status, "metacritic 404")

    def test_refresh_failed_reruns_cached_failures_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            names = ["NotFound.nds", "MissingPage.nds", "Gone.nds", "OK.nds"]
            entries = []
            for name in names:
                path = folder / name
                path.write_text("", encoding="utf-8")
                entries.append(path)

            cache = {
                str(entries[0].resolve()): sorter.Rating(
                    title="NotFound",
                    path=str(entries[0].resolve()),
                    platform="ds",
                    status="not found",
                ),
                str(entries[1].resolve()): sorter.Rating(
                    title="MissingPage",
                    path=str(entries[1].resolve()),
                    platform="ds",
                    status="metacritic 404",
                ),
                str(entries[2].resolve()): sorter.Rating(
                    title="Gone",
                    path=str(entries[2].resolve()),
                    platform="ds",
                    status="metacritic error: HTTP Error 410: Gone",
                ),
                str(entries[3].resolve()): sorter.Rating(
                    title="OK",
                    path=str(entries[3].resolve()),
                    platform="ds",
                    metacritic=80,
                    combined=80,
                    status="ok",
                ),
            }
            calls = []
            original = sorter.get_metacritic

            def fake_get_metacritic(title: str, kind: str, platform: str | None, timeout: float):
                calls.append(title)
                return 75, f"https://example.test/{title}", "ok"

            try:
                sorter.get_metacritic = fake_get_metacritic
                sorter.refresh_ratings(
                    folder,
                    entries,
                    cache,
                    "game",
                    "ds",
                    {"metacritic"},
                    refresh=False,
                    refresh_failed=True,
                    delay=0,
                    timeout=1,
                    workers=1,
                )
            finally:
                sorter.get_metacritic = original

            self.assertEqual(calls, ["NotFound", "MissingPage", "Gone"])
            self.assertEqual(cache[str(entries[3].resolve())].status, "ok")


if __name__ == "__main__":
    unittest.main()
