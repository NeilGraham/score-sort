import dataclasses
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import rom_rating_sorter as sorter


class RatingSorterTests(unittest.TestCase):
    def test_parse_args_accepts_advanced_lookup(self) -> None:
        with patch.object(sys, "argv", ["score-sort", "C:/roms", "--advanced"]):
            args = sorter.parse_args()

        self.assertTrue(args.advanced)

    def test_parse_args_accepts_debug(self) -> None:
        with patch.object(sys, "argv", ["score-sort", "C:/roms", "--debug"]):
            args = sorter.parse_args()

        self.assertTrue(args.debug)

    def test_parse_args_accepts_match_review_paths(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["score-sort", "C:/roms", "--write-match-review", "review.tsv", "--apply-match-review", "decisions.tsv"],
        ):
            args = sorter.parse_args()

        self.assertEqual(args.write_match_review, Path("review.tsv"))
        self.assertEqual(args.apply_match_review, Path("decisions.tsv"))

    def test_parse_args_rejects_removed_lookup_missing_alias(self) -> None:
        with (
            patch.object(sys, "argv", ["score-sort", "C:/roms", "--lookup-missing"]),
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as exit_context,
        ):
            sorter.parse_args()

        self.assertNotEqual(exit_context.exception.code, 0)

    def test_clean_title_removes_common_release_noise(self) -> None:
        title = sorter.clean_title(Path("Chrono_Trigger_(USA)_[Rev 1].nds"))

        self.assertEqual(title, "Chrono Trigger")

    def test_slug_candidates_include_rom_catalog_variants(self) -> None:
        self.assertIn("the-bigs-2", sorter.slug_candidates("Bigs 2, The"))
        self.assertIn("cop-the-recruit", sorter.slug_candidates("C O P - The Recruit"))
        self.assertIn("syberia", sorter.slug_candidates("B Sokal Syberia"))
        self.assertIn(
            "wwe-smackdown-vs-raw-2009",
            sorter.slug_candidates("WWE SmackDown vs Raw 2009 Featuring ECW"),
        )

    def test_match_normalization_ignores_articles_symbols_and_preserves_versions(self) -> None:
        self.assertEqual(sorter.normalize_match_text("The Chronicles of Mystery - The Secret Tree of Life"), "chronicles mystery secret tree life")
        self.assertEqual(sorter.normalize_match_text("Might & Magic - Clash of Heroes"), "might magic clash heroes")
        self.assertIn("11", sorter.match_tokens("FIFA Soccer 11"))
        self.assertTrue(sorter.has_version_conflict("Cars 2", "Cars"))

    def test_platform_match_auto_accepts_article_shift(self) -> None:
        ratings = [
            sorter.PlatformRating(
                title="The Bigs 2",
                slug="the-bigs-2",
                metacritic=76,
                metacritic_url="https://www.metacritic.com/game/the-bigs-2/",
            )
        ]

        match = sorter.platform_rating_match("Bigs 2, The", ratings)

        self.assertIsNotNone(match)
        self.assertEqual(match[0].slug, "the-bigs-2")
        self.assertEqual(match[1], "exact-title")

    def test_platform_match_does_not_auto_accept_version_conflict(self) -> None:
        ratings = [
            sorter.PlatformRating(
                title="Cars",
                slug="cars",
                metacritic=69,
                metacritic_url="https://www.metacritic.com/game/cars/",
            )
        ]

        self.assertIsNone(sorter.auto_platform_rating_match("Cars 2", ratings))
        candidates = sorter.review_platform_match_candidates("Cars 2", ratings)
        self.assertEqual(candidates[0].rating.slug, "cars")
        self.assertTrue(candidates[0].version_conflict)

    def test_platform_match_returns_borderline_review_candidate(self) -> None:
        ratings = [
            sorter.PlatformRating(
                title="Meteos - Disney Magic",
                slug="meteos-disney-magic",
                metacritic=74,
                metacritic_url="https://www.metacritic.com/game/meteos-disney-magic/",
            )
        ]

        self.assertIsNone(sorter.auto_platform_rating_match("Meteos Disney", ratings))
        candidates = sorter.review_platform_match_candidates("Meteos Disney", ratings)

        self.assertEqual(candidates[0].rating.slug, "meteos-disney-magic")
        self.assertGreaterEqual(candidates[0].score, 0.80)
        self.assertLess(candidates[0].score, 0.93)

    def test_platform_match_ignores_low_confidence_candidate(self) -> None:
        ratings = [
            sorter.PlatformRating(
                title="Chrono Trigger",
                slug="chrono-trigger",
                metacritic=92,
                metacritic_url="https://www.metacritic.com/game/chrono-trigger/",
            )
        ]

        self.assertEqual(sorter.review_platform_match_candidates("Totally Different Game", ratings), [])

    def test_sorted_ratings_puts_high_scores_first_and_unrated_last(self) -> None:
        entries = [Path("low.nds"), Path("high.nds"), Path("missing.nds")]
        rows = []
        for entry, score in zip(entries, [72, 95, None], strict=True):
            rating = sorter.Rating(title=entry.stem, path=str(entry.resolve()), metacritic=score)
            rating.calculate()
            if rating.combined is None:
                rating.status = "not found"
            rows.append(rating)

        rows = sorter.sorted_ratings(rows, entries)

        self.assertEqual([row.title for row in rows], ["high", "low", "missing"])

    def test_sorted_ratings_excludes_lookup_failures(self) -> None:
        entries = [Path("scored.nds"), Path("unscored.nds"), Path("missing-page.nds"), Path("timeout.nds")]
        rows = [
            sorter.Rating(title="scored", path=str(entries[0].resolve()), metacritic=80, combined=80),
            sorter.Rating(title="unscored", path=str(entries[1].resolve()), status="not found"),
            sorter.Rating(title="missing page", path=str(entries[2].resolve()), status="metacritic 404"),
            sorter.Rating(title="timeout", path=str(entries[3].resolve()), status="metacritic error: timed out"),
        ]

        sorted_rows = sorter.sorted_ratings(rows, entries)

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

    def test_auto_format_uses_paths_when_stdout_is_redirected(self) -> None:
        rows = [
            sorter.Rating(title="A", path="C:/media/A.nds", combined=90),
            sorter.Rating(title="B", path="C:/media/B.nds", combined=80),
        ]
        output = io.StringIO()

        with redirect_stdout(output):
            sorter.print_rankings(rows, limit=2, output_format="auto")

        self.assertEqual(output.getvalue(), "C:/media/A.nds\nC:/media/B.nds\n")

    def test_action_helpers_are_quiet_without_verbose(self) -> None:
        row = sorter.Rating(title="A", path="C:/media/A.nds", combined=90)
        stdout = io.StringIO()
        stderr = io.StringIO()
        previous_verbose = sorter.VERBOSE
        sorter.VERBOSE = False
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                sorter.delete_rows([row], yes=False)
        finally:
            sorter.VERBOSE = previous_verbose

        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_json_format_outputs_rating_objects(self) -> None:
        row = sorter.Rating(title="A", path="C:/media/A.nds", combined=90)
        output = io.StringIO()

        with redirect_stdout(output):
            sorter.print_rankings([row], output_format="json")

        self.assertEqual(json.loads(output.getvalue()), [dataclasses.asdict(row)])

    def test_table_format_includes_metacritic_user_score_column(self) -> None:
        row = sorter.Rating(
            title="User Only",
            path="C:/media/User Only.nds",
            metacritic_user=8.4,
            metacritic_user_count=25,
        )
        row.calculate()
        output = io.StringIO()

        with redirect_stdout(output):
            sorter.print_rankings([row], output_format="table")

        text = output.getvalue()
        self.assertIn("MC User", text)
        self.assertIn("8.4", text)
        self.assertIn("84.0", text)

    def test_user_only_score_needs_minimum_rating_count_to_rank(self) -> None:
        one_vote = sorter.Rating(
            title="Tiny Sample",
            path="C:/media/Tiny Sample.nds",
            metacritic_user=10.0,
            metacritic_user_count=1,
        )
        four_votes = sorter.Rating(
            title="Enough Sample",
            path="C:/media/Enough Sample.nds",
            metacritic_user=8.8,
            metacritic_user_count=4,
        )

        one_vote.calculate()
        four_votes.calculate()

        self.assertIsNone(one_vote.combined)
        self.assertEqual(four_votes.combined, 88.0)

    def test_table_format_appends_metacritic_counts_to_scores(self) -> None:
        row = sorter.Rating(
            title="Black Sigil",
            path="C:/media/Black Sigil.nds",
            metacritic=58,
            metacritic_review_count=24,
            metacritic_user=9.0,
            metacritic_user_count=5,
        )
        row.calculate()
        output = io.StringIO()

        with redirect_stdout(output):
            sorter.print_rankings([row], output_format="table")

        text = output.getvalue()
        self.assertIn("58 (24)", text)
        self.assertIn("9.0 (5)", text)
        self.assertNotIn("MC Rev", text)
        self.assertNotIn("MC Users", text)

    def test_tsv_format_appends_metacritic_counts_to_scores(self) -> None:
        row = sorter.Rating(
            title="Black Sigil",
            path="C:/media/Black Sigil.nds",
            metacritic=58,
            metacritic_review_count=24,
            metacritic_user=9.0,
            metacritic_user_count=5,
        )
        row.calculate()
        output = io.StringIO()

        with redirect_stdout(output):
            sorter.print_rankings([row], output_format="tsv")

        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], "rank\tscore\tmetacritic\tmc_user\topencritic\tkind\ttitle\tpath")
        self.assertIn("\t58 (24)\t9.0 (5)\t", lines[1])

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
        self.assertEqual(sorter.parse_metacritic_review_count(page, "ace-attorney-investigations-miles-edgeworth"), 51)

    def test_parse_metacritic_user_score_from_nuxt_reference_payload(self) -> None:
        payload = [
            {"type": 1, "title": 2, "slug": 3, "criticScoreSummary": 4, "userScoreSummary": 6},
            "game-title",
            "User Only Game",
            "user-only-game",
            {"score": 5},
            None,
            {"score": 7, "ratingCount": 8},
            8.4,
            25,
        ]
        page = (
            '<script type="application/json" data-nuxt-data="nuxt-app" '
            f'id="__NUXT_DATA__">{json.dumps(payload)}</script>'
        )

        self.assertEqual(sorter.parse_metacritic_user_score(page), 8.4)
        self.assertEqual(sorter.parse_metacritic_user_score(page, "user-only-game"), 8.4)
        self.assertIsNone(sorter.parse_metacritic_user_score(page, "different-game"))
        self.assertEqual(sorter.parse_metacritic_user_rating_count(page, "user-only-game"), 25)

    def test_parse_metacritic_user_score_ignores_countless_scaled_nuxt_score(self) -> None:
        payload = [
            {"type": 1, "title": 2, "slug": 3, "criticScoreSummary": 4, "userScoreSummary": 6},
            "game-title",
            "TBD User Game",
            "tbd-user-game",
            {"score": 5, "reviewCount": 8},
            73,
            {"score": 5},
            21,
        ]
        page = (
            '<script type="application/json" data-nuxt-data="nuxt-app" '
            f'id="__NUXT_DATA__">{json.dumps(payload)}</script>'
        )

        self.assertEqual(sorter.parse_metacritic_score(page, "tbd-user-game"), 73)
        self.assertIsNone(sorter.parse_metacritic_user_score(page, "tbd-user-game"))

    def test_parse_metacritic_user_score_from_visible_summary(self) -> None:
        page = """
        <section>
          <h3>User score</h3>
          <p>Generally favorable reviews</p>
          <p>Based on 25 User Ratings</p>
          <div>8.4</div>
        </section>
        """

        self.assertEqual(sorter.parse_metacritic_user_score(page), 8.4)

    def test_parse_metacritic_user_score_from_global_score_card(self) -> None:
        payload = [
            {"type": 1, "title": 2, "slug": 3, "criticScoreSummary": 4},
            "game-title",
            "User Score Card Game",
            "user-score-card-game",
            {"score": 5},
            84,
        ]
        page = (
            '<script type="application/json" data-nuxt-data="nuxt-app" '
            f'id="__NUXT_DATA__">{json.dumps(payload)}</script>'
            "<section>"
            "<div><div>Critic Score</div><span data-testid=\"global-score-value\">84</span></div>"
            "<div><div>User Score</div><span data-testid=\"global-score-value\">8.2</span></div>"
            "</section>"
        )

        self.assertEqual(sorter.parse_metacritic_user_score(page, "user-score-card-game"), 8.2)

    def test_parse_metacritic_user_score_does_not_inherit_visible_metascore(self) -> None:
        page = """
        <section>
          <div data-testid="global-score-header">Metascore</div>
          <a data-testid="global-score-review-count-link">Based on 21 Critic Reviews</a>
          <div data-testid="global-score-value-wrapper" title="Metascore 73 out of 100">
            <span data-testid="global-score-value">73</span>
          </div>
          <div data-testid="global-score-header">User Score</div>
          <p>tbd</p>
          <p>No user score yet</p>
        </section>
        """

        self.assertEqual(sorter.parse_metacritic_score(page, "tbd-game"), 73)
        self.assertEqual(sorter.parse_metacritic_review_count(page, "tbd-game"), 21)
        self.assertIsNone(sorter.parse_metacritic_user_score(page, "tbd-game"))
        self.assertIsNone(sorter.parse_metacritic_user_rating_count(page, "tbd-game"))

    def test_parse_metacritic_visible_global_score_card_reads_score_and_counts(self) -> None:
        page = """
        <section>
          <div data-testid="global-score-header">Metascore</div>
          <div class="flex flex-col">
            <div data-testid="global-score-sentiment">Mixed or Average</div>
            <a data-testid="global-score-review-count-link" href="/game/black-sigil-blade-of-the-exiled/critic-reviews/?platform=ds">
              Based on 24 Critic Reviews
            </a>
          </div>
          <div data-testid="global-score-value-wrapper" title="Metascore 58 out of 100" aria-label="Metascore 58 out of 100">
            <span data-testid="global-score-value">58</span>
          </div>
          <div data-testid="global-score-header">User Score</div>
          <div class="flex flex-col">
            <div data-testid="global-score-sentiment">Universal Acclaim</div>
            <a data-testid="global-score-review-count-link" href="/game/black-sigil-blade-of-the-exiled/user-reviews/?platform=ds">
              Based on 5 User Ratings
            </a>
          </div>
          <div data-testid="global-score-value-wrapper" title="User Score 9.0 out of 10" aria-label="User Score 9.0 out of 10">
            <span data-testid="global-score-value">9.0</span>
          </div>
        </section>
        """

        self.assertEqual(sorter.parse_metacritic_score(page, "black-sigil-blade-of-the-exiled"), 58)
        self.assertEqual(sorter.parse_metacritic_review_count(page, "black-sigil-blade-of-the-exiled"), 24)
        self.assertEqual(sorter.parse_metacritic_user_score(page, "black-sigil-blade-of-the-exiled"), 9.0)
        self.assertEqual(sorter.parse_metacritic_user_rating_count(page, "black-sigil-blade-of-the-exiled"), 5)

    def test_parse_metacritic_platform_review_pages_prefer_visible_platform_scores(self) -> None:
        critic_page = """
        <h1 class="subpage-header__title-text">DS Critic Reviews</h1>
        <script id="__NUXT_DATA__" type="application/json">
          [{"type":1,"title":2,"slug":3,"criticScoreSummary":4},"game-title","WWE SmackDown vs. Raw 2009","wwe-smackdown-vs-raw-2009",{"score":5,"reviewCount":6},79,47]
        </script>
        <div class="c-siteReviewScore" title="Metascore 58 out of 100" aria-label="Metascore 58 out of 100"><span>58</span></div>
        <div class="count">Showing 15 Critic Reviews</div>
        """
        user_page = """
        <h1 class="subpage-header__title-text">DS User Reviews</h1>
        <script id="__NUXT_DATA__" type="application/json">
          [{"type":1,"title":2,"slug":3,"criticScoreSummary":4,"userScoreSummary":7},"game-title","WWE SmackDown vs. Raw 2009","wwe-smackdown-vs-raw-2009",{"score":5,"reviewCount":6},79,47,{"score":8,"ratingCount":9},7.3,62]
        </script>
        <div class="c-siteReviewScore" title="User score 4.8 out of 10" aria-label="User score 4.8 out of 10"><span>4.8</span></div>
        <div class="count">Showing 3 User Reviews</div>
        """

        self.assertEqual(sorter.parse_metacritic_page_platforms(critic_page), {"nintendo-ds"})
        self.assertEqual(
            sorter.parse_metacritic_rating_values(
                critic_page,
                "wwe-smackdown-vs-raw-2009",
                prefer_visible=True,
            ),
            (58, 15, None, None),
        )
        self.assertEqual(
            sorter.parse_metacritic_rating_values(
                user_page,
                "wwe-smackdown-vs-raw-2009",
                prefer_visible=True,
            ),
            (None, None, 4.8, 3),
        )

    def test_parse_metacritic_rating_values_drops_zero_and_countless_user_scores(self) -> None:
        zero_count_critic_page = """
        <h1 class="subpage-header__title-text">DS Critic Reviews</h1>
        <div class="c-siteReviewScore" title="Metascore 96 out of 100" aria-label="Metascore 96 out of 100"><span>96</span></div>
        <div class="count">Showing 0 Critic Reviews</div>
        """
        zero_count_user_page = """
        <h1 class="subpage-header__title-text">DS User Reviews</h1>
        <div class="c-siteReviewScore" title="User score 0.3 out of 10" aria-label="User score 0.3 out of 10"><span>0.3</span></div>
        <div class="count">Showing 0 User Reviews</div>
        """
        product_page_review_snippet = """
        <div class="game-platform-logo__text">DS</div>
        <div class="c-siteReviewScore" title="Metascore 91 out of 100" aria-label="Metascore 91 out of 100"><span>91</span></div>
        <div class="c-siteReviewScore" title="User score 0 out of 10" aria-label="User score 0 out of 10"><span>0</span></div>
        """
        unavailable_product_page = """
        <div class="game-platform-logo__text">DS</div>
        <section>
          <h2>Critic Reviews</h2>
          <span>Metascore</span>
          <span>Critic reviews are not available yet</span>
          <span>tbd</span>
        </section>
        <aside>
          <div class="c-siteReviewScore" title="Metascore 96 out of 100" aria-label="Metascore 96 out of 100"><span>96</span></div>
        </aside>
        """

        self.assertEqual(
            sorter.parse_metacritic_rating_values(
                zero_count_critic_page,
                "ben-10-triple-pack",
                prefer_visible=True,
            ),
            (None, None, None, None),
        )
        self.assertEqual(
            sorter.parse_metacritic_rating_values(
                zero_count_user_page,
                "balloon-pop",
                prefer_visible=True,
            ),
            (None, None, None, None),
        )
        self.assertEqual(
            sorter.parse_metacritic_rating_values(product_page_review_snippet, "games-around-the-world"),
            (91, None, None, None),
        )
        self.assertEqual(
            sorter.parse_metacritic_rating_values(unavailable_product_page, "ben-10-triple-pack"),
            (None, None, None, None),
        )

    def test_parse_metacritic_user_score_ignores_awaiting_rating_count(self) -> None:
        page = "<h3>User Score</h3><p>tbd</p><p>No user score yet - Awaiting 3 more ratings</p>"

        self.assertIsNone(sorter.parse_metacritic_user_score(page))

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

    def test_parse_metacritic_page_platforms_reads_visible_platform_label(self) -> None:
        page = (
            '<div class="game-platform-logo__text" data-v-x>DS</div>'
            '<span class="game-platform-logo__text">Xbox Series X/S</span>'
        )

        self.assertEqual(
            sorter.parse_metacritic_page_platforms(page),
            {"nintendo-ds", "xbox-series-x"},
        )

    def test_parse_metacritic_browse_entries_reads_platform_results(self) -> None:
        payload = [
            {"type": 1, "title": 2, "slug": 3, "criticScoreSummary": 4, "userScoreSummary": 6},
            "game-title",
            "Chrono Trigger",
            "chrono-trigger",
            {"score": 5, "reviewCount": 8},
            92,
            {"score": 7, "ratingCount": 9},
            9.2,
            24,
            25,
            {"type": 10, "title": 11, "slug": 12, "criticScoreSummary": 13},
            "movie-title",
            "Chrono Trigger: The Movie",
            "chrono-trigger-the-movie",
            {"score": 14},
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
        self.assertEqual(entries[0].review_count, 24)
        self.assertEqual(entries[0].user_score, 9.2)
        self.assertEqual(entries[0].user_rating_count, 25)

    def test_parse_metacritic_browse_entries_skips_zero_review_scores(self) -> None:
        payload = [
            {"type": 1, "title": 2, "slug": 3, "criticScoreSummary": 4},
            "game-title",
            "Ben 10 Triple Pack",
            "ben-10-triple-pack",
            {"score": 5, "reviewCount": 6},
            96,
            0,
        ]
        page = (
            '<script type="application/json" data-nuxt-data="nuxt-app" '
            f'id="__NUXT_DATA__">{json.dumps(payload)}</script>'
        )

        self.assertEqual(sorter.parse_metacritic_browse_entries(page), [])

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
                review_count=22,
                user_score=9.2,
                user_score_url="https://www.metacritic.com/game/chrono-trigger/user-reviews/",
                user_rating_count=25,
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
        self.assertEqual(merged[0].review_count, 22)
        self.assertEqual(merged[0].user_score, 9.2)
        self.assertEqual(merged[0].user_rating_count, 25)
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

    def test_load_platform_ratings_skips_zero_review_scores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "platform-cache.json"
            payload = {
                "version": 1,
                "updated_at": 200,
                "platforms": {
                    "nintendo-ds": {
                        "platform": "nintendo-ds",
                        "created_at": 100,
                        "updated_at": 200,
                        "items": [
                            {
                                "title": "Ben 10 Triple Pack",
                                "slug": "ben-10-triple-pack",
                                "metacritic": 96,
                                "metacritic_url": "https://www.metacritic.com/game/ben-10-triple-pack/",
                                "review_count": 0,
                            }
                        ],
                    }
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            self.assertEqual(sorter.load_platform_ratings(path, "nintendo-ds"), [])

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
                review_count=24,
                user_score=9.2,
                user_score_url="https://www.metacritic.com/game/chrono-trigger/user-reviews/",
                user_rating_count=25,
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
                advanced_metacritic_lookup=False,
            )
        finally:
            sorter.get_metacritic = original

        self.assertEqual(rating.metacritic, 92)
        self.assertEqual(rating.metacritic_url, "https://www.metacritic.com/game/chrono-trigger/")
        self.assertEqual(rating.metacritic_review_count, 24)
        self.assertEqual(rating.metacritic_user, 9.2)
        self.assertEqual(rating.metacritic_user_count, 25)

    def test_lookup_rating_enriches_unparsed_platform_match_when_advanced_enabled(self) -> None:
        entry = Path("Meteos.nds")
        ratings = [
            sorter.PlatformRating(
                title="Meteos",
                slug="meteos",
                metacritic=88,
                metacritic_url="https://www.metacritic.com/game/meteos/",
                review_count=49,
            )
        ]
        original = sorter.get_metacritic_slug
        calls = []

        def fake_get_metacritic_slug(slug: str, kind: str, platform: str | None, timeout: float):
            calls.append((slug, kind, platform))
            return (
                88,
                "https://www.metacritic.com/game/meteos/critic-reviews/?platform=nintendo-ds",
                49,
                7.9,
                "https://www.metacritic.com/game/meteos/user-reviews/?platform=nintendo-ds",
                53,
                "ok",
            )

        try:
            sorter.get_metacritic_slug = fake_get_metacritic_slug
            rating = sorter.lookup_rating(
                1,
                1,
                entry,
                "game",
                "nintendo-ds",
                {"metacritic"},
                1,
                ratings,
                advanced_metacritic_lookup=True,
            )
        finally:
            sorter.get_metacritic_slug = original

        self.assertEqual(calls, [("meteos", "game", "nintendo-ds")])
        self.assertEqual(rating.metacritic, 88)
        self.assertEqual(rating.metacritic_review_count, 49)
        self.assertEqual(rating.metacritic_user, 7.9)
        self.assertEqual(rating.metacritic_user_count, 53)
        self.assertTrue(rating.metacritic_product_page_parsed)

    def test_lookup_rating_does_not_enrich_parsed_platform_match(self) -> None:
        entry = Path("Meteos.nds")
        ratings = [
            sorter.PlatformRating(
                title="Meteos",
                slug="meteos",
                metacritic=88,
                metacritic_url="https://www.metacritic.com/game/meteos/",
                review_count=49,
                user_score=7.9,
                user_score_url="https://www.metacritic.com/game/meteos/user-reviews/",
                user_rating_count=53,
                product_page_parsed=True,
            )
        ]
        original = sorter.get_metacritic_slug

        def unexpected_get_metacritic_slug(slug: str, kind: str, platform: str | None, timeout: float):
            raise AssertionError("already parsed product page should not be fetched")

        try:
            sorter.get_metacritic_slug = unexpected_get_metacritic_slug
            rating = sorter.lookup_rating(
                1,
                1,
                entry,
                "game",
                "nintendo-ds",
                {"metacritic"},
                1,
                ratings,
                advanced_metacritic_lookup=True,
            )
        finally:
            sorter.get_metacritic_slug = original

        self.assertEqual(rating.metacritic_user, 7.9)
        self.assertTrue(rating.metacritic_product_page_parsed)

    def test_lookup_rating_can_skip_advanced_metacritic_lookup(self) -> None:
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
                advanced_metacritic_lookup=False,
            )
        finally:
            sorter.get_metacritic = original

        self.assertIsNone(rating.metacritic)
        self.assertEqual(rating.status, "not found")

    def test_get_metacritic_returns_user_score_without_metascore(self) -> None:
        payload = [
            {"type": 1, "title": 2, "slug": 3, "criticScoreSummary": 4, "userScoreSummary": 6},
            "game-title",
            "User Only Game",
            "user-only-game",
            {"score": 5},
            None,
            {"score": 7, "ratingCount": 8},
            8.4,
            25,
        ]
        page = (
            '<div class="game-platform-logo__text">DS</div>'
            '<script type="application/json" data-nuxt-data="nuxt-app" '
            f'id="__NUXT_DATA__">{json.dumps(payload)}</script>'
        )
        empty_critic_page = """
        <h1 class="subpage-header__title-text">DS Critic Reviews</h1>
        <div class="count">Showing 0 Critic Reviews</div>
        """
        original = sorter.fetch_text
        calls: list[str] = []

        def fake_fetch_text(url: str, timeout: float) -> str:
            calls.append(url)
            if url == "https://www.metacritic.com/game/user-only-game/":
                return page
            if url.endswith("/critic-reviews/?platform=ds") or url.endswith("/critic-reviews/?platform=nintendo-ds"):
                return empty_critic_page
            raise AssertionError(f"unexpected URL: {url}")

        try:
            sorter.fetch_text = fake_fetch_text
            result = sorter.get_metacritic("User Only Game", "game", "nintendo-ds", 1)
        finally:
            sorter.fetch_text = original

        self.assertEqual(
            result,
            (
                None,
                "https://www.metacritic.com/game/user-only-game/",
                None,
                8.4,
                "https://www.metacritic.com/game/user-only-game/user-reviews/",
                25,
                "ok",
            ),
        )
        self.assertNotIn("https://www.metacritic.com/game/user-only-game/user-reviews/?platform=ds", calls)

    def test_get_metacritic_returns_visible_metascore_with_counts(self) -> None:
        page = """
        <div class="game-platform-logo__text">DS</div>
        <div data-testid="global-score-header">Metascore</div>
        <a data-testid="global-score-review-count-link" href="/game/black-sigil-blade-of-the-exiled/critic-reviews/?platform=ds">
          Based on 24 Critic Reviews
        </a>
        <div data-testid="global-score-value-wrapper" title="Metascore 58 out of 100">
          <span data-testid="global-score-value">58</span>
        </div>
        <div data-testid="global-score-header">User Score</div>
        <a data-testid="global-score-review-count-link" href="/game/black-sigil-blade-of-the-exiled/user-reviews/?platform=ds">
          Based on 5 User Ratings
        </a>
        <div data-testid="global-score-value-wrapper" title="User Score 9.0 out of 10">
          <span data-testid="global-score-value">9.0</span>
        </div>
        """
        original = sorter.fetch_text
        calls: list[str] = []

        def fake_fetch_text(url: str, timeout: float) -> str:
            calls.append(url)
            return page

        try:
            sorter.fetch_text = fake_fetch_text
            result = sorter.get_metacritic("Black Sigil - Blade of the Exiled", "game", "nintendo-ds", 1)
        finally:
            sorter.fetch_text = original

        self.assertEqual(
            result,
            (
                58,
                "https://www.metacritic.com/game/black-sigil-blade-of-the-exiled/",
                24,
                9.0,
                "https://www.metacritic.com/game/black-sigil-blade-of-the-exiled/user-reviews/",
                5,
                "ok",
            ),
        )
        self.assertEqual(calls, ["https://www.metacritic.com/game/black-sigil-blade-of-the-exiled/"])

    def test_get_metacritic_slug_merges_platform_critic_and_user_review_pages(self) -> None:
        critic_page = """
        <h1 class="subpage-header__title-text">DS Critic Reviews</h1>
        <div class="c-siteReviewScore" title="Metascore 58 out of 100" aria-label="Metascore 58 out of 100"><span>58</span></div>
        <div class="count">Showing 15 Critic Reviews</div>
        """
        user_page = """
        <h1 class="subpage-header__title-text">DS User Reviews</h1>
        <div class="c-siteReviewScore" title="User score 4.8 out of 10" aria-label="User score 4.8 out of 10"><span>4.8</span></div>
        <div class="count">Showing 3 User Reviews</div>
        """
        product_page = """
        <div class="game-platform-logo__text">Wii</div>
        <div class="game-platform-logo__text">DS</div>
        <div class="c-siteReviewScore" title="Metascore 79 out of 100" aria-label="Metascore 79 out of 100"><span>79</span></div>
        """
        original = sorter.fetch_text
        calls: list[str] = []

        def fake_fetch_text(url: str, timeout: float) -> str:
            calls.append(url)
            if url == "https://www.metacritic.com/game/wwe-smackdown-vs-raw-2009/":
                return product_page
            if url == "https://www.metacritic.com/game/wwe-smackdown-vs-raw-2009/critic-reviews/?platform=ds":
                return critic_page
            if url == "https://www.metacritic.com/game/wwe-smackdown-vs-raw-2009/user-reviews/?platform=ds":
                return user_page
            raise AssertionError(f"unexpected URL: {url}")

        try:
            sorter.fetch_text = fake_fetch_text
            result = sorter.get_metacritic_slug("wwe-smackdown-vs-raw-2009", "game", "nintendo-ds", 1)
        finally:
            sorter.fetch_text = original

        self.assertEqual(
            result,
            (
                58,
                "https://www.metacritic.com/game/wwe-smackdown-vs-raw-2009/critic-reviews/?platform=ds",
                15,
                4.8,
                "https://www.metacritic.com/game/wwe-smackdown-vs-raw-2009/user-reviews/?platform=ds",
                3,
                "ok",
            ),
        )
        self.assertEqual(
            calls,
            [
                "https://www.metacritic.com/game/wwe-smackdown-vs-raw-2009/",
                "https://www.metacritic.com/game/wwe-smackdown-vs-raw-2009/critic-reviews/?platform=ds",
                "https://www.metacritic.com/game/wwe-smackdown-vs-raw-2009/user-reviews/?platform=ds",
            ],
        )

    def test_get_metacritic_slug_ignores_ambiguous_product_page_platform_score(self) -> None:
        empty_critic_page = """
        <h1 class="subpage-header__title-text">DS Critic Reviews</h1>
        <div class="count">Showing 0 Critic Reviews</div>
        """
        empty_user_page = """
        <h1 class="subpage-header__title-text">DS User Reviews</h1>
        <div class="count">Showing 0 User Reviews</div>
        """
        ambiguous_product_page = """
        <div class="game-platform-logo__text">Wii</div>
        <div class="game-platform-logo__text">DS</div>
        <div class="c-siteReviewScore" title="Metascore 91 out of 100" aria-label="Metascore 91 out of 100"><span>91</span></div>
        """
        original = sorter.fetch_text

        def fake_fetch_text(url: str, timeout: float) -> str:
            if url == "https://www.metacritic.com/game/games-around-the-world/critic-reviews/?platform=ds":
                return empty_critic_page
            if url == "https://www.metacritic.com/game/games-around-the-world/user-reviews/?platform=ds":
                return empty_user_page
            if url == "https://www.metacritic.com/game/games-around-the-world/":
                return ambiguous_product_page
            raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)

        try:
            sorter.fetch_text = fake_fetch_text
            result = sorter.get_metacritic_slug("games-around-the-world", "game", "nintendo-ds", 1)
        finally:
            sorter.fetch_text = original

        self.assertEqual(result, (None, None, None, None, None, None, "not found"))

    def test_get_metacritic_slug_ignores_zero_review_scores(self) -> None:
        zero_critic_page = """
        <h1 class="subpage-header__title-text">DS Critic Reviews</h1>
        <div class="c-siteReviewScore" title="Metascore 96 out of 100" aria-label="Metascore 96 out of 100"><span>96</span></div>
        <div class="count">Showing 0 Critic Reviews</div>
        """
        empty_user_page = """
        <h1 class="subpage-header__title-text">DS User Reviews</h1>
        <div class="count">Showing 0 User Reviews</div>
        """
        original = sorter.fetch_text

        def fake_fetch_text(url: str, timeout: float) -> str:
            if url == "https://www.metacritic.com/game/ben-10-triple-pack/critic-reviews/?platform=ds":
                return zero_critic_page
            if url == "https://www.metacritic.com/game/ben-10-triple-pack/user-reviews/?platform=ds":
                return empty_user_page
            raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)

        try:
            sorter.fetch_text = fake_fetch_text
            result = sorter.get_metacritic_slug("ben-10-triple-pack", "game", "nintendo-ds", 1)
        finally:
            sorter.fetch_text = original

        self.assertEqual(result, (None, None, None, None, None, None, "not found"))

    def test_get_metacritic_rejects_search_result_for_wrong_platform(self) -> None:
        search_payload = [
            {"type": 1, "title": 2, "slug": 3},
            "game-title",
            "Mystery Port",
            "wrong-platform",
        ]
        game_payload = [
            {"type": 1, "title": 2, "slug": 3, "criticScoreSummary": 4},
            "game-title",
            "Mystery Port",
            "wrong-platform",
            {"score": 5},
            88,
        ]
        search_page = (
            '<script type="application/json" data-nuxt-data="nuxt-app" '
            f'id="__NUXT_DATA__">{json.dumps(search_payload)}</script>'
        )
        wrong_platform_page = (
            '<div class="game-platform-logo__text">PSP</div>'
            '<script type="application/json" data-nuxt-data="nuxt-app" '
            f'id="__NUXT_DATA__">{json.dumps(game_payload)}</script>'
        )
        original = sorter.fetch_text

        def fake_fetch_text(url: str, timeout: float) -> str:
            if "/search/" in url:
                return search_page
            if "/game/mystery-port/" in url:
                raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)
            if "/game/wrong-platform/" in url:
                return wrong_platform_page
            raise AssertionError(f"unexpected URL: {url}")

        try:
            sorter.fetch_text = fake_fetch_text
            result = sorter.get_metacritic("Mystery Port", "game", "nintendo-ds", 1)
        finally:
            sorter.fetch_text = original

        self.assertEqual(result, (None, None, None, None, None, None, "not found"))

    def test_lookup_rating_uses_metacritic_user_score_as_fallback_score(self) -> None:
        entry = Path("User Only Game.nds")
        original = sorter.get_metacritic

        def fake_get_metacritic(title: str, kind: str, platform: str | None, timeout: float):
            return (
                None,
                "https://www.metacritic.com/game/user-only-game/",
                None,
                8.4,
                "https://www.metacritic.com/game/user-only-game/user-reviews/",
                25,
                "ok",
            )

        try:
            sorter.get_metacritic = fake_get_metacritic
            rating = sorter.lookup_rating(
                1,
                1,
                entry,
                "game",
                "nintendo-ds",
                {"metacritic"},
                1,
                [],
                advanced_metacritic_lookup=True,
            )
        finally:
            sorter.get_metacritic = original

        self.assertIsNone(rating.metacritic)
        self.assertEqual(rating.metacritic_user, 8.4)
        self.assertEqual(rating.combined, 84.0)
        self.assertEqual(rating.status, "ok")

    def test_lookup_rating_uses_global_filename_miss(self) -> None:
        entry = Path("Unknown Game.nds")
        ratings = [
            sorter.PlatformRating(
                title="Known Game",
                slug="known-game",
                metacritic=80,
                metacritic_url="https://www.metacritic.com/game/known-game/",
            )
        ]
        filename_matches = {
            sorter.filename_match_key("Unknown Game"): sorter.FilenameMatch(
                title="Unknown Game",
                slug=None,
                status="not found",
                match_type="miss",
            )
        }

        rating = sorter.lookup_rating(
            1,
            1,
            entry,
            "game",
            "nintendo-ds",
            {"metacritic"},
            1,
            ratings,
            filename_matches,
            advanced_metacritic_lookup=False,
        )

        self.assertIsNone(rating.metacritic)
        self.assertEqual(rating.status, "not found")

    def test_lookup_rating_uses_global_filename_miss_during_advanced_lookup(self) -> None:
        entry = Path("Unknown Game.nds")
        ratings = [
            sorter.PlatformRating(
                title="Known Game",
                slug="known-game",
                metacritic=80,
                metacritic_url="https://www.metacritic.com/game/known-game/",
            )
        ]
        filename_matches = {
            sorter.filename_match_key("Unknown Game"): sorter.FilenameMatch(
                title="Unknown Game",
                slug=None,
                status="not found",
                match_type="miss",
                lookup_version=sorter.FILENAME_MATCH_LOOKUP_VERSION,
            )
        }
        original = sorter.get_metacritic

        def unexpected_get_metacritic(title: str, kind: str, platform: str | None, timeout: float):
            raise AssertionError("cached filename miss should not be searched again")

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
                filename_matches,
                advanced_metacritic_lookup=True,
            )
        finally:
            sorter.get_metacritic = original

        self.assertIsNone(rating.metacritic)
        self.assertEqual(rating.status, "not found")
        self.assertFalse(sorter.used_uncached_lookup(rating))

    def test_lookup_rating_retries_stale_filename_miss_during_advanced_lookup(self) -> None:
        entry = Path("Unknown Game.nds")
        filename_matches = {
            sorter.filename_match_key("Unknown Game"): sorter.FilenameMatch(
                title="Unknown Game",
                slug=None,
                status="not found",
                match_type="miss",
                lookup_version=0,
            )
        }
        original = sorter.get_metacritic
        calls = []

        def fake_get_metacritic(
            title: str,
            kind: str,
            platform: str | None,
            timeout: float,
            timings=None,
            excluded_slugs=None,
        ):
            calls.append(title)
            return (58, "https://www.metacritic.com/game/unknown-game/critic-reviews/?platform=ds", 15, None, None, None, "ok")

        try:
            sorter.get_metacritic = fake_get_metacritic
            rating = sorter.lookup_rating(
                1,
                1,
                entry,
                "game",
                "nintendo-ds",
                {"metacritic"},
                1,
                [],
                filename_matches,
                advanced_metacritic_lookup=True,
            )
        finally:
            sorter.get_metacritic = original

        self.assertEqual(calls, ["Unknown Game"])
        self.assertEqual(rating.metacritic, 58)

    def test_lookup_rating_uses_global_filename_match_before_fuzzy(self) -> None:
        entry = Path("Different Release Name.nds")
        ratings = [
            sorter.PlatformRating(
                title="Chrono Trigger",
                slug="chrono-trigger",
                metacritic=92,
                metacritic_url="https://www.metacritic.com/game/chrono-trigger/",
            )
        ]
        filename_matches = {
            sorter.filename_match_key("Different Release Name"): sorter.FilenameMatch(
                title="Different Release Name",
                slug="chrono-trigger",
                status="ok",
                match_type="matched",
            )
        }

        rating = sorter.lookup_rating(
            1,
            1,
            entry,
            "game",
            "nintendo-ds",
            {"metacritic"},
            1,
            ratings,
            filename_matches,
            advanced_metacritic_lookup=False,
        )

        self.assertEqual(rating.metacritic, 92)

    def test_lookup_rating_collects_borderline_catalog_review_candidate(self) -> None:
        entry = Path("Meteos Disney.nds")
        ratings = [
            sorter.PlatformRating(
                title="Meteos - Disney Magic",
                slug="meteos-disney-magic",
                metacritic=74,
                metacritic_url="https://www.metacritic.com/game/meteos-disney-magic/",
            )
        ]

        rating = sorter.lookup_rating(
            1,
            1,
            entry,
            "game",
            "nintendo-ds",
            {"metacritic"},
            1,
            ratings,
            advanced_metacritic_lookup=False,
        )

        review_candidates = sorter.rating_match_review_candidates(rating)
        self.assertEqual(rating.status, "not found")
        self.assertEqual(review_candidates[0].candidate_slug, "meteos-disney-magic")

    def test_lookup_rating_low_confidence_catalog_miss_uses_advanced_search(self) -> None:
        entry = Path("Unknown Game.nds")
        ratings = [
            sorter.PlatformRating(
                title="Chrono Trigger",
                slug="chrono-trigger",
                metacritic=92,
                metacritic_url="https://www.metacritic.com/game/chrono-trigger/",
            )
        ]
        original = sorter.get_metacritic
        calls = []

        def fake_get_metacritic(title: str, kind: str, platform: str | None, timeout: float):
            calls.append(title)
            return (70, "https://www.metacritic.com/game/unknown-game/", 12, None, None, None, "ok")

        try:
            sorter.get_metacritic = fake_get_metacritic
            rating = sorter.lookup_rating(
                1,
                1,
                entry,
                "game",
                "nintendo-ds",
                {"metacritic"},
                1,
                ratings,
                advanced_metacritic_lookup=True,
            )
        finally:
            sorter.get_metacritic = original

        self.assertEqual(calls, ["Unknown Game"])
        self.assertEqual(rating.metacritic, 70)

    def test_write_match_review_outputs_tsv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.tsv"
            candidates = [
                sorter.MatchReviewCandidate(
                    local_title="Meteos Disney",
                    candidate_title="Meteos - Disney Magic",
                    candidate_slug="meteos-disney-magic",
                    score=0.9,
                    reason="local-title-subset",
                )
            ]

            sorter.write_match_review(path, candidates)

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "local_title\tcandidate_title\tcandidate_slug\tscore\treason\tdecision")
            self.assertIn("Meteos Disney\tMeteos - Disney Magic\tmeteos-disney-magic\t0.9000\tlocal-title-subset\t", lines[1])

    def test_apply_match_review_accept_seeds_manual_match_that_overrides_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.tsv"
            path.write_text(
                "\n".join(
                    [
                        "local_title\tcandidate_title\tcandidate_slug\tscore\treason\tdecision",
                        "Other Name\tChrono Trigger\tchrono-trigger\t0.8500\tfuzzy\taccept",
                    ]
                ),
                encoding="utf-8",
            )
            ratings = [
                sorter.PlatformRating(
                    title="Other Name",
                    slug="other-name",
                    metacritic=70,
                    metacritic_url="https://www.metacritic.com/game/other-name/",
                ),
                sorter.PlatformRating(
                    title="Chrono Trigger",
                    slug="chrono-trigger",
                    metacritic=92,
                    metacritic_url="https://www.metacritic.com/game/chrono-trigger/",
                ),
            ]
            matches: dict[str, sorter.FilenameMatch] = {}

            changed = sorter.apply_match_review(path, matches, ratings)
            rating = sorter.lookup_rating(
                1,
                1,
                Path("Other Name.nds"),
                "game",
                "nintendo-ds",
                {"metacritic"},
                1,
                ratings,
                matches,
                advanced_metacritic_lookup=False,
            )

        self.assertTrue(changed)
        self.assertEqual(matches[sorter.filename_match_key("Other Name")].match_type, "manual")
        self.assertEqual(rating.metacritic, 92)
        self.assertEqual(rating.metacritic_url, "https://www.metacritic.com/game/chrono-trigger/")

    def test_apply_match_review_reject_prevents_that_catalog_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.tsv"
            path.write_text(
                "\n".join(
                    [
                        "local_title\tcandidate_title\tcandidate_slug\tscore\treason\tdecision",
                        "LEGO Lord Rings\tLEGO The Lord of the Rings\tlego-the-lord-of-the-rings\t1.0000\texact-title\treject",
                    ]
                ),
                encoding="utf-8",
            )
            ratings = [
                sorter.PlatformRating(
                    title="LEGO The Lord of the Rings",
                    slug="lego-the-lord-of-the-rings",
                    metacritic=80,
                    metacritic_url="https://www.metacritic.com/game/lego-the-lord-of-the-rings/",
                )
            ]
            matches: dict[str, sorter.FilenameMatch] = {}

            changed = sorter.apply_match_review(path, matches, ratings)
            rating = sorter.lookup_rating(
                1,
                1,
                Path("LEGO Lord Rings.nds"),
                "game",
                "nintendo-ds",
                {"metacritic"},
                1,
                ratings,
                matches,
                advanced_metacritic_lookup=False,
            )

        self.assertTrue(changed)
        self.assertEqual(rating.status, "not found")
        self.assertIn("lego-the-lord-of-the-rings", matches[sorter.filename_match_key("LEGO Lord Rings")].rejected_slugs)
        sorter.update_filename_matches_from_ratings(matches, [rating], ratings)
        self.assertEqual(matches[sorter.filename_match_key("LEGO Lord Rings")].status, "reviewed")

    def test_rejected_catalog_candidate_is_excluded_from_advanced_search(self) -> None:
        entry = Path("LEGO Lord Rings.nds")
        ratings = [
            sorter.PlatformRating(
                title="LEGO The Lord of the Rings",
                slug="lego-the-lord-of-the-rings",
                metacritic=80,
                metacritic_url="https://www.metacritic.com/game/lego-the-lord-of-the-rings/",
            )
        ]
        matches = {
            sorter.filename_match_key("LEGO Lord Rings"): sorter.FilenameMatch(
                title="LEGO Lord Rings",
                slug=None,
                status="reviewed",
                match_type="manual-reject",
                rejected_slugs=["lego-the-lord-of-the-rings"],
            )
        }
        original = sorter.get_metacritic
        excluded = []

        def fake_get_metacritic(
            title: str,
            kind: str,
            platform: str | None,
            timeout: float,
            timings=None,
            excluded_slugs=None,
        ):
            excluded.append(excluded_slugs)
            return (None, None, None, None, None, None, "not found")

        try:
            sorter.get_metacritic = fake_get_metacritic
            rating = sorter.lookup_rating(
                1,
                1,
                entry,
                "game",
                "nintendo-ds",
                {"metacritic"},
                1,
                ratings,
                matches,
                advanced_metacritic_lookup=True,
            )
        finally:
            sorter.get_metacritic = original

        self.assertEqual(rating.status, "not found")
        self.assertEqual(excluded, [{"lego-the-lord-of-the-rings"}])

    def test_lookup_rating_uses_cached_filename_scores_without_advanced_lookup(self) -> None:
        entry = Path("Black Sigil - Blade of the Exiled.nds")
        filename_matches = {
            sorter.filename_match_key("Black Sigil - Blade of the Exiled"): sorter.FilenameMatch(
                title="Black Sigil - Blade of the Exiled",
                slug="black-sigil-blade-of-the-exiled",
                status="ok",
                match_type="lookup",
                metacritic=58,
                metacritic_url="https://www.metacritic.com/game/black-sigil-blade-of-the-exiled/critic-reviews/?platform=nintendo-ds",
                review_count=24,
                user_score=9.0,
                user_score_url="https://www.metacritic.com/game/black-sigil-blade-of-the-exiled/user-reviews/?platform=nintendo-ds",
                user_rating_count=5,
            )
        }
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
                filename_matches,
                advanced_metacritic_lookup=False,
            )
        finally:
            sorter.get_metacritic = original

        self.assertEqual(rating.metacritic, 58)
        self.assertEqual(rating.metacritic_review_count, 24)
        self.assertEqual(rating.metacritic_user, 9.0)
        self.assertEqual(rating.metacritic_user_count, 5)
        self.assertEqual(rating.combined, 58)

    def test_lookup_rating_merges_cached_user_score_into_platform_match(self) -> None:
        entry = Path("Chrono Trigger.nds")
        ratings = [
            sorter.PlatformRating(
                title="Chrono Trigger",
                slug="chrono-trigger",
                metacritic=92,
                metacritic_url="https://www.metacritic.com/game/chrono-trigger/",
                review_count=57,
            )
        ]
        filename_matches = {
            sorter.filename_match_key("Chrono Trigger"): sorter.FilenameMatch(
                title="Chrono Trigger",
                slug="chrono-trigger",
                status="ok",
                match_type="matched",
                metacritic=91,
                metacritic_url="https://www.metacritic.com/game/chrono-trigger/",
                review_count=55,
                user_score=9.2,
                user_score_url="https://www.metacritic.com/game/chrono-trigger/user-reviews/",
                user_rating_count=100,
            )
        }

        rating = sorter.lookup_rating(
            1,
            1,
            entry,
            "game",
            "nintendo-ds",
            {"metacritic"},
            1,
            ratings,
            filename_matches,
            advanced_metacritic_lookup=False,
        )

        self.assertEqual(rating.metacritic, 92)
        self.assertEqual(rating.metacritic_review_count, 57)
        self.assertEqual(rating.metacritic_user, 9.2)
        self.assertEqual(rating.metacritic_user_count, 100)

    def test_refresh_ratings_with_workers_returns_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            entries = []
            for name in ["Low.nds", "High.nds"]:
                path = folder / name
                path.write_text("", encoding="utf-8")
                entries.append(path)
            original = sorter.get_metacritic

            def fake_get_metacritic(title: str, kind: str, platform: str | None, timeout: float):
                return (90 if title == "High" else 50), f"https://example.test/{title}", "ok"

            try:
                sorter.get_metacritic = fake_get_metacritic
                rows = sorter.refresh_ratings(
                    entries,
                    "game",
                    "ds",
                    {"metacritic"},
                    delay=0,
                    timeout=1,
                    workers=2,
                )
            finally:
                sorter.get_metacritic = original

            self.assertEqual(sorted(row.metacritic for row in rows), [50, 90])

    def test_refresh_ratings_does_not_delay_for_platform_catalog_only_matching(self) -> None:
        ratings = [
            sorter.PlatformRating(
                title="Chrono Trigger",
                slug="chrono-trigger",
                metacritic=92,
                metacritic_url="https://www.metacritic.com/game/chrono-trigger/",
                user_score=9.2,
                user_score_url="https://www.metacritic.com/game/chrono-trigger/user-reviews/",
            )
        ]

        with patch.object(sorter.time, "sleep") as sleep:
            rows = sorter.refresh_ratings(
                [Path("Chrono Trigger.nds"), Path("Unknown Game.nds")],
                "game",
                "nintendo-ds",
                {"metacritic"},
                delay=1,
                timeout=1,
                workers=1,
                platform_ratings=ratings,
                advanced_metacritic_lookup=False,
            )

        self.assertEqual([row.metacritic for row in rows], [92, None])
        sleep.assert_not_called()

    def test_refresh_ratings_does_not_delay_for_fully_cached_advanced_matches(self) -> None:
        ratings = [
            sorter.PlatformRating(
                title="Chrono Trigger",
                slug="chrono-trigger",
                metacritic=92,
                metacritic_url="https://www.metacritic.com/game/chrono-trigger/",
                user_score=9.2,
                user_score_url="https://www.metacritic.com/game/chrono-trigger/user-reviews/",
                user_rating_count=100,
                product_page_parsed=True,
            )
        ]
        original = sorter.get_metacritic_slug

        def unexpected_get_metacritic_slug(slug: str, kind: str, platform: str | None, timeout: float):
            raise AssertionError("already parsed product page should not be fetched")

        try:
            sorter.get_metacritic_slug = unexpected_get_metacritic_slug
            with patch.object(sorter.time, "sleep") as sleep:
                rows = sorter.refresh_ratings(
                    [Path("Chrono Trigger.nds")],
                    "game",
                    "nintendo-ds",
                    {"metacritic"},
                    delay=1,
                    timeout=1,
                    workers=1,
                    platform_ratings=ratings,
                    advanced_metacritic_lookup=True,
                )
        finally:
            sorter.get_metacritic_slug = original

        self.assertEqual(rows[0].metacritic, 92)
        self.assertEqual(rows[0].metacritic_user, 9.2)
        self.assertFalse(sorter.used_uncached_lookup(rows[0]))
        sleep.assert_not_called()

    def test_update_filename_matches_from_ratings_promotes_match(self) -> None:
        matches: dict[str, sorter.FilenameMatch] = {}
        platform_ratings = [
            sorter.PlatformRating(
                title="Chrono Trigger",
                slug="chrono-trigger",
                metacritic=92,
                metacritic_url="https://www.metacritic.com/game/chrono-trigger/",
            )
        ]
        rows = [
            sorter.Rating(
                title="Chrono Trigger",
                path="C:/roms/Chrono Trigger.nds",
                metacritic=92,
                metacritic_url="https://www.metacritic.com/game/chrono-trigger/",
                metacritic_review_count=57,
                metacritic_user=9.2,
                metacritic_user_url="https://www.metacritic.com/game/chrono-trigger/user-reviews/",
                metacritic_user_count=100,
                combined=92,
            )
        ]

        changed = sorter.update_filename_matches_from_ratings(matches, rows, platform_ratings)

        self.assertTrue(changed)
        match = matches[sorter.filename_match_key("Chrono Trigger")]
        self.assertEqual(match.slug, "chrono-trigger")
        self.assertEqual(match.metacritic, 92)
        self.assertEqual(match.review_count, 57)
        self.assertEqual(match.user_score, 9.2)
        self.assertEqual(match.user_rating_count, 100)

    def test_update_filename_matches_from_ratings_caches_lookup_only_match(self) -> None:
        matches: dict[str, sorter.FilenameMatch] = {}
        rows = [
            sorter.Rating(
                title="Black Sigil - Blade of the Exiled",
                path="C:/roms/Black Sigil - Blade of the Exiled.nds",
                metacritic=58,
                metacritic_url="https://www.metacritic.com/game/black-sigil-blade-of-the-exiled/critic-reviews/?platform=nintendo-ds",
                metacritic_review_count=24,
                metacritic_user=9.0,
                metacritic_user_url="https://www.metacritic.com/game/black-sigil-blade-of-the-exiled/user-reviews/?platform=nintendo-ds",
                metacritic_user_count=5,
                metacritic_product_page_parsed=True,
                combined=58,
            )
        ]

        changed = sorter.update_filename_matches_from_ratings(matches, rows, [])

        self.assertTrue(changed)
        match = matches[sorter.filename_match_key("Black Sigil - Blade of the Exiled")]
        self.assertEqual(match.slug, "black-sigil-blade-of-the-exiled")
        self.assertEqual(match.match_type, "lookup")
        self.assertEqual(match.metacritic, 58)
        self.assertEqual(match.review_count, 24)
        self.assertEqual(match.user_score, 9.0)
        self.assertEqual(match.user_rating_count, 5)
        self.assertTrue(match.product_page_parsed)

    def test_update_platform_ratings_from_rows_marks_product_page_parsed(self) -> None:
        platform_ratings = [
            sorter.PlatformRating(
                title="Meteos",
                slug="meteos",
                metacritic=88,
                metacritic_url="https://www.metacritic.com/game/meteos/",
                review_count=49,
            )
        ]
        rows = [
            sorter.Rating(
                title="Meteos",
                path="C:/roms/Meteos.nds",
                metacritic=88,
                metacritic_url="https://www.metacritic.com/game/meteos/critic-reviews/?platform=nintendo-ds",
                metacritic_review_count=49,
                metacritic_user=7.9,
                metacritic_user_url="https://www.metacritic.com/game/meteos/user-reviews/?platform=nintendo-ds",
                metacritic_user_count=53,
                metacritic_product_page_parsed=True,
                combined=88,
            )
        ]

        changed = sorter.update_platform_ratings_from_rows(platform_ratings, rows)

        self.assertTrue(changed)
        self.assertTrue(platform_ratings[0].product_page_parsed)
        self.assertEqual(platform_ratings[0].user_score, 7.9)
        self.assertEqual(platform_ratings[0].user_rating_count, 53)

    def test_update_filename_matches_from_ratings_promotes_miss(self) -> None:
        matches: dict[str, sorter.FilenameMatch] = {}
        rows = [
            sorter.Rating(
                title="Unknown Game",
                path="C:/roms/Unknown Game.nds",
                status="not found",
            )
        ]

        changed = sorter.update_filename_matches_from_ratings(matches, rows, [])

        self.assertTrue(changed)
        self.assertEqual(matches[sorter.filename_match_key("Unknown Game")].status, "not found")
        self.assertIsNone(matches[sorter.filename_match_key("Unknown Game")].slug)
        self.assertEqual(
            matches[sorter.filename_match_key("Unknown Game")].lookup_version,
            sorter.FILENAME_MATCH_LOOKUP_VERSION,
        )

    def test_save_filename_matches_uses_global_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "platform-cache.json"
            sorter.save_platform_ratings(
                path,
                "nintendo-ds",
                [
                    sorter.PlatformRating(
                        title="Chrono Trigger",
                        slug="chrono-trigger",
                        metacritic=92,
                        metacritic_url="https://www.metacritic.com/game/chrono-trigger/",
                    )
                ],
                created_at=1,
            )
            matches = {
                sorter.filename_match_key("Chrono Trigger (USA)"): sorter.FilenameMatch(
                    title="Chrono Trigger",
                    slug="chrono-trigger",
                    status="ok",
                    match_type="matched",
                    metacritic=92,
                    metacritic_url="https://www.metacritic.com/game/chrono-trigger/",
                    review_count=57,
                    user_score=9.2,
                    user_score_url="https://www.metacritic.com/game/chrono-trigger/user-reviews/",
                    user_rating_count=100,
                    product_page_parsed=True,
                )
            }

            sorter.save_filename_matches(path, "nintendo-ds", matches)
            loaded = sorter.load_filename_matches(path, "nintendo-ds")

            self.assertEqual(loaded[sorter.filename_match_key("Chrono Trigger (USA)")].slug, "chrono-trigger")
            self.assertEqual(loaded[sorter.filename_match_key("Chrono Trigger (USA)")].metacritic, 92)
            self.assertEqual(loaded[sorter.filename_match_key("Chrono Trigger (USA)")].review_count, 57)
            self.assertEqual(loaded[sorter.filename_match_key("Chrono Trigger (USA)")].user_score, 9.2)
            self.assertEqual(loaded[sorter.filename_match_key("Chrono Trigger (USA)")].user_rating_count, 100)
            self.assertTrue(loaded[sorter.filename_match_key("Chrono Trigger (USA)")].product_page_parsed)

    def test_load_filename_matches_drops_suspicious_countless_user_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "platform-cache.json"
            payload = {
                "version": 1,
                "updated_at": 200,
                "platforms": {
                    "nintendo-ds": {
                        "platform": "nintendo-ds",
                        "created_at": 100,
                        "updated_at": 200,
                        "items": [],
                        "filename_matches": {
                            "tbd-game": {
                                "title": "TBD Game",
                                "slug": "tbd-game",
                                "status": "ok",
                                "match_type": "lookup",
                                "metacritic": 73,
                                "metacritic_url": "https://www.metacritic.com/game/tbd-game/",
                                "review_count": 21,
                                "user_score": 7.3,
                                "user_score_url": "https://www.metacritic.com/game/tbd-game/user-reviews/",
                                "user_rating_count": None,
                                "lookup_version": sorter.FILENAME_MATCH_LOOKUP_VERSION,
                                "updated_at": 150,
                            },
                            "user-only": {
                                "title": "User Only",
                                "slug": "user-only",
                                "status": "ok",
                                "match_type": "lookup",
                                "metacritic": None,
                                "metacritic_url": None,
                                "review_count": None,
                                "user_score": 7.6,
                                "user_score_url": "https://www.metacritic.com/game/user-only/user-reviews/",
                                "user_rating_count": None,
                                "lookup_version": sorter.FILENAME_MATCH_LOOKUP_VERSION,
                                "updated_at": 150,
                            },
                            "zero-critic": {
                                "title": "Zero Critic",
                                "slug": "zero-critic",
                                "status": "ok",
                                "match_type": "manual",
                                "metacritic": 96,
                                "metacritic_url": "https://www.metacritic.com/game/zero-critic/",
                                "review_count": 0,
                                "user_score": None,
                                "user_score_url": None,
                                "user_rating_count": None,
                                "lookup_version": sorter.FILENAME_MATCH_LOOKUP_VERSION,
                                "updated_at": 150,
                            },
                        },
                    }
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            loaded = sorter.load_filename_matches(path, "nintendo-ds")

            self.assertIsNone(loaded["tbd-game"].user_score)
            self.assertIsNone(loaded["tbd-game"].user_score_url)
            self.assertIsNone(loaded["user-only"].user_score)
            self.assertIsNone(loaded["user-only"].user_score_url)
            self.assertIsNone(loaded["zero-critic"].metacritic)
            self.assertIsNone(loaded["zero-critic"].metacritic_url)
            self.assertIsNone(loaded["zero-critic"].review_count)

    def test_load_filename_matches_expires_misses_older_than_platform_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "platform-cache.json"
            payload = {
                "version": 1,
                "updated_at": 200,
                "platforms": {
                    "nintendo-ds": {
                        "platform": "nintendo-ds",
                        "created_at": 200,
                        "updated_at": 200,
                        "items": [],
                        "filename_matches": {
                            "old-miss": {
                                "title": "Old Miss",
                                "slug": None,
                                "status": "not found",
                                "match_type": "miss",
                                "updated_at": 100,
                            },
                            "old-hit": {
                                "title": "Old Hit",
                                "slug": "old-hit",
                                "status": "ok",
                                "match_type": "matched",
                                "updated_at": 100,
                            },
                        },
                    }
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            loaded = sorter.load_filename_matches(path, "nintendo-ds")

            self.assertNotIn("old-miss", loaded)
            self.assertIn("old-hit", loaded)

    def test_load_filename_matches_drops_stale_lookup_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "platform-cache.json"
            payload = {
                "version": 1,
                "updated_at": 200,
                "platforms": {
                    "nintendo-ds": {
                        "platform": "nintendo-ds",
                        "created_at": 100,
                        "updated_at": 200,
                        "items": [],
                        "filename_matches": {
                            "stale-lookup": {
                                "title": "Stale Lookup",
                                "slug": "stale-lookup",
                                "status": "ok",
                                "match_type": "lookup",
                                "metacritic": 91,
                                "metacritic_url": "https://www.metacritic.com/game/stale-lookup/",
                                "lookup_version": sorter.FILENAME_MATCH_LOOKUP_VERSION - 1,
                                "updated_at": 150,
                            },
                            "manual": {
                                "title": "Manual",
                                "slug": "manual",
                                "status": "ok",
                                "match_type": "manual",
                                "metacritic": 80,
                                "metacritic_url": "https://www.metacritic.com/game/manual/",
                                "lookup_version": sorter.FILENAME_MATCH_LOOKUP_VERSION - 1,
                                "updated_at": 150,
                            },
                        },
                    }
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")

            loaded = sorter.load_filename_matches(path, "nintendo-ds")

            self.assertNotIn("stale-lookup", loaded)
            self.assertIn("manual", loaded)


if __name__ == "__main__":
    unittest.main()
