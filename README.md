# Score Sort

Rank game, movie, or TV files and first-level folders by Metacritic scores.
For games with a platform selected or inferred, Score Sort first uses
Metacritic's all-time platform catalog, then optionally performs deeper product
page lookups with `--advanced`.

Platform catalogs and filename matches are cached system-wide:

```powershell
%LOCALAPPDATA%\score-sort\metacritic_platforms.json
```

On macOS/Linux:

```sh
${XDG_CACHE_HOME:-~/.cache}/score-sort/metacritic_platforms.json
```

## Setup

```powershell
uv run score-sort --help
uv run python -m unittest discover -s tests
```

## Usage

Rank a folder:

```powershell
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)"
```

Force a fresh platform catalog lookup:

```powershell
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --refresh
```

Sort by low score first, by name, or by reverse name:

```powershell
uv run score-sort "D:\Games\Nintendo Switch" --sort score --direction asc
uv run score-sort "D:\Games\Nintendo Switch" --sort name
uv run score-sort "D:\Games\Nintendo Switch" --sort name --direction desc
```

Filter to a score range:

```powershell
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --range "1-50"
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --range ">70"
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --range "<=40"
```

Override detection when needed:

```powershell
uv run score-sort "D:\Media\Movies" --kind movie
uv run score-sort "D:\Media\TV Shows" --kind tv
uv run score-sort "D:\Games\PSX" --kind game --platform ps1
```

Use parallel lookup workers and advanced Metacritic product-page enrichment:

```powershell
uv run score-sort "D:\sorted-roms\Nintendo - Nintendo DS (Decrypted)" --workers 6 --advanced
```

## CLI

The CLI intentionally keeps a small surface:

```text
folder
--refresh
--sort {score,name}
--direction {asc,desc}
--range RANGE
--kind {game,movie,tv}
--platform PLATFORM
--workers WORKERS
--advanced
```

First-level child folders are scanned automatically alongside known media file
extensions, so there is no separate directory-scanning flag.

## Project Structure

```text
src/score_sort/
  cli.py        command-line parsing and orchestration
  core.py       rating models, title matching, Metacritic parsers, cache helpers
  __main__.py   enables python -m score_sort
```

## Notes

Context detection walks up from the scanned folder through its parents. It
recognizes media folders such as `Movies`, `TV Shows`, `Games`, and `ROMs`, plus
common platform names like `ds`, `ps3`, `psx`, `gba`, and longer folder names
such as `Nintendo - Nintendo DS (Decrypted)`.

Filename cleaning strips known ROM/media extensions and compound container
suffixes such as `.nkit.iso`, `.xiso.iso`, and `.nkit.rvz` before matching.

Supported Metacritic platform slugs include `3ds`, `dreamcast`,
`game-boy-advance`, `gamecube`, `meta-quest`, `mobile`, `nintendo-64`,
`nintendo-ds`, `nintendo-switch`, `nintendo-switch-2`, `pc`, `ps-vita`, `ps1`,
`ps2`, `ps3`, `ps4`, `ps5`, `psp`, `wii`, `wii-u`, `xbox`, `xbox-360`,
`xbox-one`, and `xbox-series-x`.
