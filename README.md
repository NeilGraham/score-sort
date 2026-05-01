# Media Rating Sorter

Small Python utility for ranking game, movie, or TV files and first-level
folders by critic scores from Metacritic and, for games only when available,
OpenCritic. For games with a platform selected, the script now fetches
Metacritic's all-time platform browse catalog first, then matches local ROM
filenames against those platform ratings.

The script keeps per-folder ROM matches in `.rom_rating_cache.json` inside the
scanned folder. Platform-wide Metacritic catalogs are stored in a system-wide
cache so any folder containing games for the same platform can reuse the parsed
ratings.

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
uv run score-sort "D:\Games\Nintendo Switch" --order worst --format paths --limit 10 |
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

Metacritic does not provide a simple public API, and OpenCritic's coverage is
thinner for older console and handheld libraries. This script therefore uses
Metacritic browse pages for platform-wide game catalogs, then fuzzy-matches ROM
filenames against that cached catalog. It does not hit individual Metacritic
game pages for catalog misses unless you pass `--lookup-missing`. OpenCritic's
public web endpoint is still used when requested. Platform catalogs are cached
system-wide and per-file matches are cached locally. Platform catalogs refresh
after 30 days by default; use `--platform-cache-days` to change that, or
`--refresh` to rebuild both local matches and the system platform catalog.
Failed or missing per-file matches are cached so a large library can resume
cleanly. Timed-out lookups are retried on later runs, while 404 and not-found
results stay cached. Use `--refresh-failed` to retry every cached failure
without refreshing successful entries.

Each platform catalog has a platform-level `created_at` timestamp. Each catalog
entry stores the critic score, Metacritic URL, and entry `updated_at`. Future
user-score scraping can store `user_score`, `user_score_url`, and
`user_score_updated_at` on the same entry. When a platform catalog is rebuilt,
critic scores are refreshed while existing user-score fields are preserved, so
you can compare `user_score_updated_at` with the platform `created_at` to find
user scores that are older than the current critic catalog.

Supported Metacritic platform slugs are `3ds`, `dreamcast`,
`game-boy-advance`, `gamecube`, `meta-quest`, `mobile`, `nintendo-64`,
`nintendo-ds`, `nintendo-switch`, `nintendo-switch-2`, `pc`, `ps-vita`, `ps1`,
`ps2`, `ps3`, `ps4`, `ps5`, `psp`, `wii`, `wii-u`, `xbox`, `xbox-360`,
`xbox-one`, and `xbox-series-x`.

The script prints each URL as it works. If a site is slow or blocking requests,
lower the per-request timeout, for example `--timeout 4`. You can also reduce
the pause between uncached lookups with `--delay 0.25`, though being too
aggressive may make public sites more likely to reject requests.
