# score-sort

Rank your ROMs, movies, or TV shows by their Metacritic score.

Point it at a folder and it prints a sorted table — highest-rated first.

```
pip install score-sort
score-sort "D:\ROMs\Nintendo DS"
```

Platform and media type are detected automatically from the folder name, so most of the time that's all you need.

## Examples

```sh
score-sort "D:\ROMs\Nintendo DS"
score-sort "D:\ROMs\PlayStation 2"
score-sort "D:\Media\Movies"
score-sort "D:\Media\TV Shows"
```

Filter to only the good stuff:

```sh
score-sort "D:\ROMs\Game Boy Advance" --range ">75"
score-sort "D:\ROMs\Nintendo DS" --range "1-50"
```

Sort alphabetically instead of by score:

```sh
score-sort "D:\ROMs\Nintendo Switch" --sort name
```

Override detection when the folder name isn't enough:

```sh
score-sort "D:\ROMs\PSX" --platform ps1
score-sort "D:\Media\Films" --kind movie
```

## Options

| Flag | Description |
|---|---|
| `--range RANGE` | Filter by score: `>70`, `>=70`, `<50`, `<=50`, `1-50` |
| `--sort {score,name}` | Sort column (default: `score`) |
| `--direction {asc,desc}` | Sort direction (default: `desc` for score, `asc` for name) |
| `--kind {game,movie,tv}` | Override media type detection |
| `--platform PLATFORM` | Override platform detection (see slugs below) |
| `--workers N` | Parallel lookup workers (default: 1) |
| `--advanced` | Enrich results with full Metacritic product-page lookups |
| `--refresh` | Force a fresh catalog download, ignoring the cache |

## Supported platforms

`3ds` · `dreamcast` · `game-boy-advance` · `gamecube` · `meta-quest` · `mobile` · `nintendo-64` · `nintendo-ds` · `nintendo-switch` · `nintendo-switch-2` · `pc` · `ps-vita` · `ps1` · `ps2` · `ps3` · `ps4` · `ps5` · `psp` · `wii` · `wii-u` · `xbox` · `xbox-360` · `xbox-one` · `xbox-series-x`

## How it works

For games with a recognized platform, score-sort downloads the full Metacritic catalog for that platform and matches your filenames against it. Results are cached so repeat runs are instant. With `--advanced`, it also hits individual product pages for any titles that didn't match the catalog.

Movies and TV shows skip the catalog step and go straight to product-page lookups.

## Cache

Platform catalogs are cached system-wide and refreshed automatically after 30 days.

**Windows:** `%LOCALAPPDATA%\score-sort\metacritic_platforms.json`  
**macOS / Linux:** `${XDG_CACHE_HOME:-~/.cache}/score-sort/metacritic_platforms.json`

Force a refresh with `--refresh`.
