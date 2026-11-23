"""Copy to SSD's job (`mf app`): a mashup's files into their folder on the SSD, so that a cancel never leaves part
of a file there. Each file is copied with ditto to a hidden .part-<name> beside its target and then renamed over
it (one volume, so the rename is all or nothing); Ctrl-C, which is the app's Cancel, removes the temp. Then
`dot_clean -m` removes the AppleDouble ._ files macOS writes on FAT and exFAT drives: in that folder, and the
own ._ file beside it of that folder and of each parent folder the copy made (no other ._ file).

Usage: python ssd_copy.py SRC_DIR DST_DIR ITEM...   (ITEM: a file or folder in SRC_DIR). Standard library only."""
import os, subprocess, sys

DITTO = ["ditto", "--norsrc", "--noextattr", "--noqtn"]


def files(src, items):
    """Paths relative to src: each item, or every file in it if it is a folder (no ._ files)."""
    for item in items:
        p = os.path.join(src, item)
        if not os.path.isdir(p):
            yield item
            continue
        for d, _, names in sorted(os.walk(p)):
            yield from (os.path.relpath(os.path.join(d, n), src) for n in sorted(names) if not n.startswith("._"))


def put(src, out, copy):
    """copy(src, temp) beside out, then the temp renamed to out; the temp is removed if anything stops it."""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    part = os.path.join(os.path.dirname(out), ".part-" + os.path.basename(out))
    try:
        copy(src, part)
        os.replace(part, out)
    finally:
        if os.path.exists(part):
            os.remove(part)


def ditto(src, dst):
    subprocess.run(DITTO + [src, dst], check=True)


def made_by(dst):
    """dst and each of its parent folders that does not exist yet: the folders this copy makes."""
    out, d = [], os.path.abspath(dst)
    while not os.path.exists(d):
        out.append(d)
        d = os.path.dirname(d)
    return out or [os.path.abspath(dst)]


def main(src, dst, *items, copy=ditto):
    own = made_by(dst)
    for rel in files(src, items):
        put(os.path.join(src, rel), os.path.join(dst, rel), copy)
        print(f"copied {rel}", flush=True)
    subprocess.run(["dot_clean", "-m", dst], check=True)
    for d in own:                                       # each such folder's own ._ file, beside it
        f = os.path.join(os.path.dirname(d), "._" + os.path.basename(d))
        if os.path.isfile(f):
            os.remove(f)
    print(f"done: {dst}")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    try:
        main(*sys.argv[1:])
    except KeyboardInterrupt:
        raise SystemExit("cancelled: no part-copied file left behind")
