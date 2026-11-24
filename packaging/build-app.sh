#!/bin/bash
# Build "Mashup Studio.app" and "Mashup Studio-<version>.dmg" into dist/ (macOS 14+, Apple Silicon):
#
#   packaging/build-app.sh [--sign] [--notarize] [--no-dmg] [--lock]
#
# The app is small. It holds the Studio's code (the public tree of the last commit: uncommitted changes are not in it),
# packaging/requirements.lock (every Python package, pinned with hashes, which the app installs on first open), the
# bundled uv, ffmpeg, ffprobe and rubberband with their licences, and its own executable (packaging/app/main.swift).
# The third-party files are listed with their sha256 in packaging/third-party.txt, downloaded into build/cache, and
# refused unless the checksum matches; each archive is unpacked in a folder of its own and only the listed file is
# taken. Every program must be arm64 and load nothing outside /usr/lib and /System.
#   --sign      sign the programs inside-out, then the app (and the DMG): Developer ID, hardened runtime, secure
#               timestamp. No entitlements: Python runs outside the app, so nothing inside needs one. A signed build
#               refuses uncommitted changes to its inputs and never writes over a DMG of the same version.
#   --notarize  (implies --sign) notarize and staple the app, then the DMG, with xcrun notarytool.
#   --no-dmg    stop after the app.
#   --lock      first write packaging/requirements.lock again from pyproject.toml (after changing a pin).
# Environment: SIGN_IDENTITY and NOTARY_PROFILE (required for --sign and --notarize); MANIFEST, BUILD_CACHE and DIST
# for tests.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
manifest="${MANIFEST:-$here/packaging/third-party.txt}"
cache="${BUILD_CACHE:-$here/build/cache}"
out="${DIST:-$here/dist}"
identity="${SIGN_IDENTITY:-}"
profile="${NOTARY_PROFILE:-}"
sign=0 notarize=0 dmg=1 relock=0
for a in "$@"; do
  case "$a" in
    --sign) sign=1 ;;
    --notarize) sign=1 notarize=1 ;;
    --no-dmg) dmg=0 ;;
    --lock) relock=1 ;;
    *) echo "usage: packaging/build-app.sh [--sign] [--notarize] [--no-dmg] [--lock]" >&2; exit 2 ;;
  esac
done
die() { echo "build-app: $*" >&2; exit 1; }
[ $sign = 0 ] || [ $relock = 0 ] || die "--lock rewrites packaging/requirements.lock: run it alone, commit the lock, then sign"
[ $sign = 0 ] || [ -n "$identity" ] || die "--sign needs SIGN_IDENTITY (\"Developer ID Application: Your Name (TEAMID)\")"
[ $notarize = 0 ] || [ -n "$profile" ] || die "--notarize needs NOTARY_PROFILE (a notarytool keychain profile)"
version=$(sed -n 's/^version = "\([0-9][0-9.]*\)"$/\1/p' "$here/pyproject.toml")
[ -n "$version" ] || die "no version = \"x.y.z\" in pyproject.toml"
app="$out/Mashup Studio.app"
res="$app/Contents/Resources"
dmgfile="$out/Mashup Studio-$version.dmg"
# The public tree (nothing personal) in the author's repo; in the public repo, which has no such script, its HEAD.
tree_script="$here/scripts/make-public-tree.sh"
[ -x "$tree_script" ] || tree_script=""
if [ $sign = 1 ]; then                                # a release: exactly what is committed, and nothing written over
  # The code comes from HEAD; these are the files the build reads from the working tree. Untracked files (a source
  # install's own briefs) are not inputs.
  changed=$(git -C "$here" status --porcelain --untracked-files=no -- packaging scripts pyproject.toml)
  [ -z "$changed" ] || die "a signed build needs its inputs committed; changed: $changed"
  [ ! -e "$dmgfile" ] || die "$dmgfile already exists: move it away first (it may be the notarized one)"
fi
work=$(mktemp -d "${TMPDIR:-/tmp}/mashup-studio-build.XXXXXX")
trap 'rm -rf "$work"' EXIT
mkdir -p "$out" "$cache"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$res/bin" "$res/app" "$res/licenses"

# ---------- Third-party files: download once, check, take only the listed file ----------
while read -r dest sha url member; do
  case "$dest" in ''|\#*) continue ;; esac
  case "$dest/$member" in *..*|/*) die "manifest: $dest $member leaves the app folder" ;; esac
  file="$cache/$sha"
  if [ ! -f "$file" ]; then
    echo "downloading $url"
    curl -fsSL --retry 2 -o "$file.part" "$url" || die "could not download $url"
    mv "$file.part" "$file"
  fi
  got=$(shasum -a 256 "$file" | cut -c1-64)
  if [ "$got" != "$sha" ]; then
    rm -f "$file"
    die "sha256 mismatch for $url: got $got, packaging/third-party.txt says $sha (the file was removed from the cache)"
  fi
  mkdir -p "$(dirname "$res/$dest")"
  if [ "$member" = - ]; then
    cp "$file" "$res/$dest"
  else
    x=$(mktemp -d "$work/unpack.XXXXXX")
    case "$url" in
      *.zip) ditto -x -k "$file" "$x" ;;
      *.tar.gz|*.tgz) tar -xzf "$file" -C "$x" ;;
      *.tar.bz2) tar -xjf "$file" -C "$x" ;;
      *) die "manifest: no way to unpack $url" ;;
    esac
    [ -f "$x/$member" ] && [ ! -L "$x/$member" ] || die "$member is not a file in $url"
    cp "$x/$member" "$res/$dest"
  fi
done < "$manifest"

# ---------- The Python packages the app installs: every one pinned with its hashes, in step with pyproject.toml ----------
lockfile="$here/packaging/requirements.lock"
if [ $relock = 1 ]; then                    # wheels only, as setup installs them, and no uv.toml of the developer's
  MACOSX_DEPLOYMENT_TARGET=14.0 "$res/bin/uv" pip compile "$here/pyproject.toml" --no-config --only-binary :all: \
    --generate-hashes --python-version 3.12 --python-platform aarch64-apple-darwin \
    --custom-compile-command "packaging/build-app.sh --lock" -o "$lockfile"
fi
# Every dependency must be a plain "name==version" pin, and the lock must hold exactly that pin.
pins=0
while read -r dep; do
  [[ $dep =~ ^\"([A-Za-z0-9_.-]+)==([0-9][A-Za-z0-9.+!-]*)\",?$ ]] ||
    die "pyproject.toml: $dep is not a plain \"name==version\" pin; the app installs packaging/requirements.lock, so pin every dependency, then run packaging/build-app.sh --lock"
  name=$(printf '%s' "${BASH_REMATCH[1]}" | tr '[:upper:]' '[:lower:]' | sed 's/[_.]/-/g')
  grep -qixF -- "$name==${BASH_REMATCH[2]} \\" "$lockfile" ||
    die "packaging/requirements.lock does not pin $name==${BASH_REMATCH[2]}: after changing the dependencies, run packaging/build-app.sh --lock"
  pins=$((pins + 1))
done < <(sed -n '/^dependencies = \[/,/^\]/p' "$here/pyproject.toml" | sed '1d;$d;s/#.*//;s/[[:space:]]//g' | grep -v '^$')
[ $pins -gt 0 ] || die "no dependencies found in pyproject.toml"

# ---------- The Studio's code ----------
mkdir "$work/tree"
if [ -n "$tree_script" ]; then
  "$tree_script" "$work/tree"
else
  git -C "$here" archive HEAD | tar -x -C "$work/tree"
fi
for f in mashup_finder templates briefs pyproject.toml LICENSE README.md; do
  [ -e "$work/tree/$f" ] && cp -R "$work/tree/$f" "$res/app/"
done
[ -f "$res/app/mashup_finder/cli.py" ] || die "the code is missing from the app"
cp "$lockfile" "$res/app/requirements.lock"
cp "$here/packaging/licenses/SOURCES.md" "$res/licenses/SOURCES.md"
cp "$here/packaging/launcher/AppIcon.icns" "$res/AppIcon.icns"
sed "s/@VERSION@/$version/g" "$here/packaging/app/Info.plist" > "$app/Contents/Info.plist"
plutil -lint "$app/Contents/Info.plist" >/dev/null || die "Info.plist does not parse"
xcrun swiftc -O -swift-version 5 -target arm64-apple-macos14.0 -o "$app/Contents/MacOS/mashup-studio" "$here/packaging/app/main.swift"

# ---------- Every program: arm64 only, and nothing loaded from outside the system ----------
for f in "$res/bin/"* "$app/Contents/MacOS/mashup-studio"; do
  chmod 755 "$f"
  archs=$(lipo -archs "$f" 2>/dev/null) || die "$f is not a Mach-O program"
  arm=$(printf '%s\n' $archs | grep -x arm64 || printf '%s\n' $archs | grep -x arm64e) || true   # plain arm64 first
  [ -n "$arm" ] || die "$f has no arm64 code ($archs)"
  if [ "$archs" != "$arm" ]; then lipo -thin "$arm" "$f" -output "$f.thin" && mv -f "$f.thin" "$f"; fi   # arm64 only
  outside=$(otool -L "$f" | tail -n +2 | awk '{print $1}' | grep -vE '^(/usr/lib/|/System/Library/)' || true)
  [ -z "$outside" ] || die "$f loads libraries from outside the system: $outside"
done
# Nothing personal in the app: the names check over all of Resources except bin/, whose programs are upstream's own
# bytes, checked by sha256 above (their help texts contain words the check would flag).
if [ -n "$tree_script" ]; then
  for d in "$res/app" "$res/licenses"; do "$tree_script" --check "$d"; done
fi
[ "$(cd "$res" && ls | sort | tr '\n' ' ')" = "AppIcon.icns app bin licenses " ] || die "Resources holds more than app, bin, licenses and the icon"
xattr -cr "$app"
echo "built $app ($version, $(du -sh "$app" | cut -f1))"

# ---------- Sign and notarize (Mark's Developer ID; run only on his word) ----------
if [ $sign = 1 ]; then
  for f in "$res/bin/"* "$app/Contents/MacOS/mashup-studio"; do
    codesign --force --options runtime --timestamp --sign "$identity" "$f"
  done
  codesign --force --options runtime --timestamp --sign "$identity" "$app"
  codesign --verify --deep --strict --verbose=2 "$app"
fi
if [ $notarize = 1 ]; then
  ditto -c -k --keepParent "$app" "$work/app.zip"
  xcrun notarytool submit "$work/app.zip" --keychain-profile "$profile" --wait
  xcrun stapler staple "$app" || die "stapling the app failed"
fi

# ---------- The DMG: the app and a link to /Applications ----------
if [ $dmg = 1 ]; then
  mkdir "$work/dmg"
  ditto "$app" "$work/dmg/Mashup Studio.app"
  ln -s /Applications "$work/dmg/Applications"
  rm -f "$dmgfile"
  hdiutil create -volname "Mashup Studio" -srcfolder "$work/dmg" -fs HFS+ -format UDZO -ov "$dmgfile" -quiet
  if [ $sign = 1 ]; then codesign --force --timestamp --sign "$identity" "$dmgfile"; fi
  if [ $notarize = 1 ]; then
    xcrun notarytool submit "$dmgfile" --keychain-profile "$profile" --wait
    xcrun stapler staple "$dmgfile" || die "stapling the DMG failed"
  fi
  echo "built $dmgfile ($(du -h "$dmgfile" | cut -f1))"
fi
if [ $notarize = 1 ]; then
  spctl -a -vv -t exec "$app"
  [ $dmg = 0 ] || spctl -a -vv -t open --context context:primary-signature "$dmgfile"
fi
