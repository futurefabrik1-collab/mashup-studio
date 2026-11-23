# What is in Mashup Studio.app, and where it comes from

Mashup Studio itself is MIT-licensed (`app/LICENSE`). The app also carries three programs, unchanged, in `bin/`.

| Program | Version | Licence | Binary we ship | Its exact source |
|---|---|---|---|---|
| ffmpeg, ffprobe | 9.1, static arm64 build by Martin Riedl | GPL version 3 (`ffmpeg/COPYING.GPLv3`); the build's configure line and the version of every library in it are in `ffmpeg/build.txt` | https://ffmpeg.martin-riedl.de/download/macos/arm64/1795207104_9.1/ffmpeg.zip and `ffprobe.zip` | https://ffmpeg.org/releases/ffmpeg-9.1.tar.xz; the build script: https://git.martin-riedl.de/ffmpeg/build-script (commit 78d7b7d9ff) |
| rubberband | 4.0.0, command-line tool by Breakfast Quay (thinned to arm64) | GPL (`rubberband/COPYING`) | https://breakfastquay.com/files/releases/rubberband-4.0.0-gpl-executable-macos.tar.bz2 | https://breakfastquay.com/files/releases/rubberband-4.0.0.tar.bz2 |
| uv | 0.13.9, by Astral | MIT or Apache-2.0 (`uv/LICENSE-MIT`, `uv/LICENSE-APACHE`) | https://github.com/astral-sh/uv/releases/download/0.13.9/uv-aarch64-apple-darwin.tar.gz | https://github.com/astral-sh/uv/tree/0.13.9 |

The first time it opens, the app uses uv to download Python 3.12 (python-build-standalone) and the Python packages listed in `app/requirements.lock` (each pinned with its sha256) from PyPI into `~/Library/Application Support/Mashup Studio`. Each comes under its own licence, and none of them is inside the app.
