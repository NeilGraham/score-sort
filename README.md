# Media Rating Sorter

Small Python utility for ranking game, movie, or TV files and first-level
folders by critic scores from Metacritic and, for games only when available,
OpenCritic. For games with a platform selected, the script now fetches
Metacritic's all-time platform browse catalog first, then matches local ROM
filenames against those platform ratings.

The script keeps platform-wide Metacritic catalogs and filename matches in a
system-wide cache, so any folder containing games for the same platform can
reuse the parsed ratings and prior filename matches.

On Windows, the system platform cache defaults to:

```powershell
%LOCALAPPDATA%\score-sort\metacritic_platforms.json
```

On macOS/Linux, it defaults to:

```sh
${XDG_CACHE_HOME:-~/.cache}/score-sort/metacritic_platforms.json
```

## Setup

This is a `uv` project with a console command named `score-sort`.

```powershell
uv run score-sort --help
uv run python -m unittest discover -s tests
```

## Examples

Rank a Nintendo DS folder. The script can infer `nintendo-ds` from the folder
name, so `--platform nintendo-ds` is optional here. By default it only uses
Metacritic, which is usually best for older systems:

```powershell
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)"
```

Use both Metacritic and OpenCritic for newer game libraries:

```powershell
uv run score-sort "D:\Games\Nintendo Switch" --sources both
```

Show only the top 50:

```powershell
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --limit 50
```

Print only paths for the top 25 so another command can consume them:

```powershell
uv run score-sort "D:\Games\Nintendo Switch" --format paths --limit 25
```

When stdout is piped or redirected, the default `--format auto` also prints
only paths:

```powershell
uv run score-sort "D:\Games\Nintendo Switch" --limit 25 |
  ForEach-Object { Write-Host "Would handle $_" }
```

Print only paths for the worst 10 rated items:

```powershell
uv run score-sort "D:\Games\Nintendo Switch" --order worst --format paths --limit 10
```

Show only games in a score range:

```powershell
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --range "1-50"
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --range ">70"
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --range "<=40"
```

Pipe the worst rated paths into your own PowerShell action:

```powershell
uv run score-sort "D:\Games\Nintendo Switch" --order worst --limit 10 |
  ForEach-Object { Write-Host "Would handle $_" }
```

Small test run before scanning a big folder:

```powershell
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --max-entries 5 --timeout 4
```

Smoke-test the Metacritic platform parser against only the first browse page:

```powershell
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --platform nintendo-ds --catalog-max-pages 1
```

Refresh a big folder faster with parallel lookups:

```powershell
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --refresh --workers 6 --timeout 8
```

Use advanced Metacritic lookups to enrich catalog matches from product pages
and search for entries missing from the platform catalog:

```powershell
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --advanced --workers 6
```

Write borderline catalog matches to a TSV for manual review, then apply accepted
or rejected decisions on the next run:

```powershell
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --advanced --write-match-review "D:\sorted-roms\ds-match-review.tsv"
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --advanced --apply-match-review "D:\sorted-roms\ds-match-review.tsv"
```

Dry-run deleting the bottom 20 rated games:

```powershell
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --action delete-bottom --count 20
```

Dry-run deleting the bottom 20 rated games within a range:

```powershell
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --range "1-50" --action delete-bottom --count 20
```

Actually delete the bottom 20 after reviewing the dry-run:

```powershell
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --action delete-bottom --count 20 --yes
```

Dry-run moving the top 50 rated games:

```powershell
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --action move-top --count 50 --dest "D:\best-roms\Nintendo DS"
```

Actually move the top 50:

```powershell
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --action move-top --count 50 --dest "D:\best-roms\Nintendo DS" --yes
```

Action commands print the selected file paths on stdout, so they can be piped
directly into another command. Add `--verbose` to see dry-run/action status and
detailed lookup URLs on stderr.

Rank movie folders under a `Movies` directory:

```powershell
uv run score-sort "D:\Media\Movies" --include-dirs
```

Rank TV show folders under a `TV Shows` directory:

```powershell
uv run score-sort "D:\Media\TV Shows" --include-dirs
```

Override detection when needed:

```powershell
uv run score-sort "D:\Media\Shows" --kind tv --include-dirs
uv run score-sort "D:\Games\PSX" --kind game --platform ps1
```

## Notes

Context detection walks up from the scanned folder through its parents. It looks
for media folders like `Movies`, `TV Shows`, `Films`, `Series`, `Games`, and
`ROMs`, plus simple platform folders like `ds`, `ps3`, `psx`, `gba`, and longer
names like `Nintendo - Nintendo DS (Decrypted)`.

Filename cleaning strips normal scan extensions plus known compound container
suffixes such as `.nkit.iso`, `.xiso.iso`, and `.nkit.rvz` before matching, so
those format markers do not become part of the Metacritic search or filename
cache key.

Metacritic does not provide a simple public API, and OpenCritic's coverage is
thinner for older console and handheld libraries. This script therefore uses
Metacritic browse pages for platform-wide game catalogs, then matches ROM
filenames against that cached catalog before doing any per-title search.
Catalog matching auto-accepts exact slug/title matches and very high-confidence
fuzzy matches, while borderline candidates can be written with
`--write-match-review`. The review TSV has `local_title`, `candidate_title`,
`candidate_slug`, `score`, `reason`, and `decision`; set `decision` to `accept`
or `reject`, then pass it back with `--apply-match-review`. Accepted decisions
are cached as manual filename matches, and rejected candidate slugs are skipped
for that exact cleaned filename without blocking other search results.

The script does not hit individual Metacritic game pages unless you pass
`--advanced`; when it does, it verifies that the fetched game page declares the
requested platform, then reads both the critic Metascore and the Metacritic user
score, along with critic review and user-rating counts when Metacritic exposes
them. `--advanced` also enriches already-matched catalog entries whose product
page has not been checked yet. Product-page attempts that return no extra data
are cached too, so already-checked entries and cached filename misses are reused
without another product-page or search request. The table and TSV
outputs append counts to the `MC` and `MC User` values, such as `58 (24)` or
`9.0 (5)`. If a game has no critic score but does have a Metacritic user score,
that user score is normalized to 100 points as the row's fallback `Score` so it
can still be sorted and filtered.
OpenCritic's public web endpoint is still used when requested. Platform catalogs
and filename matches are cached system-wide. Platform catalogs refresh after 30
days by default; use `--platform-cache-days` to change that, or `--refresh` to
rebuild the system platform catalog. Successful fuzzy filename matches and
advanced lookup results are saved per platform in the same global platform
cache, including any Metacritic score, user score, and counts found during the
lookup, so other folders with the same cleaned filename can resolve immediately
even without `--advanced`. Not-found filename misses are cached too. Misses
older than the platform catalog's `created_at` are ignored, which gives a
refreshed catalog a chance to resolve games that were previously missing.

Each platform catalog has a platform-level `created_at` timestamp. Each catalog
entry stores the critic score, review count, Metacritic URL, and entry
`updated_at`. The platform also has a `filename_matches` map from cleaned
filenames to Metacritic slugs. Product-page scraping stores `user_score`,
`user_score_url`, `user_rating_count`, `user_score_updated_at`, and
`product_page_parsed` on the same catalog entry. Missing `product_page_parsed`
is treated as false. When a platform catalog is rebuilt, critic scores are
refreshed while existing user-score fields and filename matches are preserved,
so you can compare `user_score_updated_at` with the platform `created_at` to
find user scores that are older than the current critic catalog.

Supported Metacritic platform slugs are `3ds`, `dreamcast`,
`game-boy-advance`, `gamecube`, `meta-quest`, `mobile`, `nintendo-64`,
`nintendo-ds`, `nintendo-switch`, `nintendo-switch-2`, `pc`, `ps-vita`, `ps1`,
`ps2`, `ps3`, `ps4`, `ps5`, `psp`, `wii`, `wii-u`, `xbox`, `xbox-360`,
`xbox-one`, and `xbox-series-x`.

When stderr is attached to a terminal, long Metacritic work shows Rich progress
bars for platform catalog pages and entry/product-page processing. Use
`--verbose` to also print each URL as it works, or `--debug` to print per-entry
timings split across matching, fetch, parse, fill, and total time. If a site is
slow or blocking requests, lower the per-request timeout, for example
`--timeout 4`. You can also reduce the pause between uncached lookups with
`--delay 0.25`, though being too aggressive may make public sites more likely to
reject requests.
