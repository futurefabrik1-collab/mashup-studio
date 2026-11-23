# Mashup Studio

Mashup Studio finds pairs of tracks in your own music library that mash up: the vocal of one track over the
instrumental of another. You audition the pairs, keep the good ones, and Mashup Studio arranges full-length mashups
that you can edit block by block and export. It runs on your Mac as a local web page. Your music stays on your
computer.

## Requirements

- A Mac with Apple Silicon and macOS 14 or later.
- [Homebrew](https://brew.sh), for ffmpeg and Rubber Band.
- git.
- Disk space for the Python packages (PyTorch is the largest), the separation model (about 80 MB, downloaded on
  first use) and the stem cache (about 90 MB per track, capped at 8 GB).

## Install

```bash
git clone https://github.com/futurefabrik1-collab/mashup-studio.git
cd mashup-studio
packaging/install.sh
```

The installer does three things:

1. It installs ffmpeg and Rubber Band with Homebrew, if they are missing.
2. It makes a Python 3.12 environment in `~/Library/Application Support/Mashup Studio/venv` with
   [uv](https://docs.astral.sh/uv/) (installed first if it is missing) and installs this folder into it.
3. It writes `Mashup Studio.app` into `~/Applications`.

Keep the cloned folder: the app runs from it, and your briefs and runs are stored in it. To update, run `git pull`
and then `packaging/install.sh` again.

## First start

Open **Mashup Studio** from `~/Applications`. A Terminal window starts the Studio, and the browser opens its
settings page:

1. **Music folders**: the folders with your music (mp3, m4a, aiff, wav or flac). Mashup Studio only reads them.
2. **Export folder**: where exported pairs and finished mashups go. An external drive is fine. Choose a new or
   empty folder, not one with music in it: scans leave the export folder out. If you change it later, move the old
   one out of your music folders first, or the next scan reads its files as music.
3. Select **Save**, then **Scan now**. The scan reads each file's tags. Tracks without a BPM tag get a tempo from
   Beat This!, and tracks without a key tag get a key estimate. The next scan reads only new and changed files.
4. Select **Open the Studio**.

To change the folders or scan new music later, select **Settings** in the Studio.

To stop the Studio, select **Quit** in the page or close its Terminal window. Opening the app while the Studio runs
opens the page again.

## Use the Studio

- **Briefs**: describe a gig or a job: tempos, genres, years, words to include or leave out, or one song to find
  partners for. A live count shows how many pairs the brief finds. **Run** finds the pairs and checks the best ones
  with audio: stem separation, beat grid, key fit, and a short preview.
- **Runs**: every run, with how many pairs passed the check, are kept and have mashups.
- **Audition**: play the previews and keep or skip each pair (keys: `K`, `S`, arrow keys, `Space`).
  **Export kept** copies the original tracks and their separated stems into pair folders in the export folder.
- **Mashups**: **Make** arranges a full-length mashup of a kept pair from a template (club, dj-tool or short).
  **Copy to export folder** puts the mashup (`mashup.wav`, `arrangement.json` and an Ableton folder) into the pair's
  folder. Rate mashups with stars; later searches favour pairs like the ones you rated high.
- **Editor**: cut and fade the blocks of a mashup, then render it again.

Long work runs as jobs, one at a time, with progress at the top of the page and a Cancel button.

## Command line

`mf` in the cloned folder runs the same steps without the page:

| Command | What it does |
|---|---|
| `./mf scan [FOLDER ...] [--catalog CSV]` | Scan music folders into the catalog (default: the folders and catalog in the settings) |
| `./mf find --brief briefs/90s-mashup.toml` | Find pairs (also `--seed TEXT`, `--genre TEXT`, `--limit N`) |
| `./mf check RUN --top 15` | Check the top pairs of a run with audio and write previews |
| `./mf run --brief FILE --top 10` | Find and check in one go |
| `./mf mashup RUN PAIR_ID --template club` | Arrange a full-length mashup |
| `./mf edit MASHUP_FOLDER` | Open the block editor |
| `./mf export RUN --picks PICKS.json` | Export kept pairs (default: into the export folder) |
| `./mf app` | Start the Studio (`--port N`, `--no-browser`) |

`briefs/90s-mashup.toml` is an example brief. `mashup_finder/criteria.py` lists every filter a brief can use.

## Settings

The settings file is `~/Library/Application Support/Mashup Studio/config.toml`. The settings page writes it; you can
also edit it, then select **Restart** in the Studio. To use another file, set `MF_CONFIG` to its path.

| Setting | Meaning |
|---|---|
| `music_folders` | Folders `mf scan` reads (a list) |
| `catalog` | The catalog CSV that searches read and `mf scan` writes (default: `catalog.csv` beside the settings file) |
| `export_folder` | Where Export and Copy to export folder put pair folders (default: `~/Music/Mashup Studio`) |
| `cache_folder` | Separated stems and beat grids (default: `_cache` in the cloned folder) |
| `extra_sources` | Optional analysis files with vocal presence and key confidence per track (a list) |
| `playlists_folder` | Optional folder of `.m3u8` playlists that the brief drafter can suggest |

`mf scan` never writes over a catalog CSV that it did not write itself. It stops if a music folder is missing, or
has no audio left where the catalog has tracks, so an unplugged drive cannot empty the catalog. It leaves out the
export and cache folders, also when they sit inside a music folder.

## Draft briefs with Claude (optional)

In **Briefs**, **+ New brief** turns a few sentences ("140 and 180, jungle and breaks, 80s and 90s vocals, no
neuro") into a brief. If the [Claude Code](https://claude.com/claude-code) command-line tool is installed and you
are logged in (run `claude` once in Terminal), the words go to Claude through that login. Otherwise Mashup Studio
writes a simple draft from the numbers and the "no ..." words, and says so. Mashup Studio stores no API key.

## Run the tests

```bash
"$HOME/Library/Application Support/Mashup Studio/venv/bin/python" -m unittest discover -s tests -t .
```

The tests make their own audio with ffmpeg, write only to temporary folders, read none of your settings, and need
no network or model weights. They take about six minutes. `node tests/pagecheck.mjs` also checks the pages in
Google Chrome (Node 22 or later).

## Credits and licences

Mashup Studio is released under the MIT License (see `LICENSE`). It builds on:

- [Demucs](https://github.com/adefossez/demucs) (MIT) by Alexandre Défossez, for stem separation.
  The `htdemucs` model weights are **not cleared for commercial use**: use Mashup Studio for personal projects.
- [Beat This!](https://github.com/CPJKU/beat_this) (MIT) by Francesco Foscarin, Jan Schlüter and Gerhard Widmer
  (CPJKU), for beats, downbeats and tempo.
- [Rubber Band](https://breakfastquay.com/rubberband/) (GPL) by Breakfast Quay, for time stretching and pitch
  shifting. Mashup Studio runs it as the separate `rubberband` program.
- [FFmpeg](https://ffmpeg.org) (LGPL/GPL), for reading tags and decoding and encoding audio. Mashup Studio runs it as
  the separate `ffmpeg` and `ffprobe` programs.
- [librosa](https://librosa.org) (ISC) and [PyTorch](https://pytorch.org) (BSD).

Spleeter is not used.
