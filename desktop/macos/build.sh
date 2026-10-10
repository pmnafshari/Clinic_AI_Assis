#!/bin/sh
# Builds Clinic Workspace.app and its .dmg from a clean checkout (P28). Run from the repository root:
#     desktop/macos/build.sh [OUT_DIR]          (default ../macos-builds)
# Needs: Xcode command line tools (swiftc, codesign, hdiutil), uv (for the relocatable CPython), network access to
# PyPI for the pinned runtime packages, and the clinic's own .venv only to build the synthetic demo snapshot. The
# result never depends on this checkout or its .venv at run time.
set -eu
ROOT=$(pwd)
[ -f run.py ] && [ -d desktop/macos ] || { echo "run from the repository root"; exit 1; }
VERSION=$(cat desktop/macos/VERSION)
COMMIT=$(git rev-parse --short HEAD)
BUILD=$(git rev-list --count HEAD)
DIRTY=$(git status --porcelain | grep -v '^??' | head -1 || true)
[ -z "$DIRTY" ] || { echo "the tree has uncommitted changes - commit first so the build matches a commit"; exit 1; }
OUT=${1:-$ROOT/../macos-builds}
PYVER=3.12.11
APP_NAME="Clinic Workspace"
WORK=$(mktemp -d /private/tmp/clinic-workspace-build.XXXX)
APP="$WORK/$APP_NAME.app"
C="$APP/Contents"
R="$C/Resources"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$C/MacOS" "$R" "$C/Library/LaunchAgents" "$OUT"
echo "== Clinic Workspace $VERSION ($BUILD, $COMMIT)"

echo "== native code"
swiftc -O -target arm64-apple-macos14.0 desktop/macos/Sources/*.swift -o "$C/MacOS/ClinicWorkspace"
swiftc -O -target arm64-apple-macos14.0 desktop/macos/Helper/ClinicServiceHelper.swift -o "$C/MacOS/ClinicServiceHelper"
sed -e "s/@VERSION@/$VERSION/" -e "s/@BUILD@/$BUILD/" -e "s/@COMMIT@/$COMMIT/" desktop/macos/Info.plist > "$C/Info.plist"
cp desktop/macos/local.clinicdemo.workspace.clinic-service.plist "$C/Library/LaunchAgents/"

echo "== python $PYVER (python-build-standalone via uv)"
uv python install "$PYVER" >/dev/null
PYSRC=$(uv python find "$PYVER" | xargs dirname | xargs dirname)
cp -R "$PYSRC" "$R/python"
rm -rf "$R/python/lib/python3.12/test" "$R/python/lib/python3.12/idlelib" "$R/python/lib/python3.12/tkinter" \
       "$R/python/lib/tcl8" "$R/python/lib/tcl8.6" "$R/python/lib/tk8.6" "$R/python/lib/itcl4.2.4" "$R/python/lib/thread2.8.9"
rm -f "$R/python/lib/python3.12/EXTERNALLY-MANAGED"     # uv's marker; this copy is the app's own interpreter
"$R/python/bin/python3.12" -m pip install --quiet --disable-pip-version-check --no-warn-script-location \
    --no-compile -r desktop/macos/requirements-bundle.lock
"$R/python/bin/python3.12" -m pip check

echo "== clinic code (git archive of $COMMIT, without tests and planning)"
mkdir -p "$R/backend"
git archive HEAD | tar -x -C "$R/backend"
(cd "$R/backend" && rm -rf desktop notebooks docs .github models/stt models/jarvis scratchpad \
    *_selftest.py e2e_*.py eval_*.py && find . -name '*_selftest.py' -delete)
cp desktop/macos/backend/workspace_backend.py "$R/backend/"
"$R/python/bin/python3.12" -m compileall -q "$R/backend" "$R/python/lib/python3.12" >/dev/null || true

echo "== synthetic demo snapshot"
.venv/bin/python desktop/macos/backend/build_demo_seed.py "$R/demo-seed"

echo "== local search model (Apache-2.0, already cached by the project; never downloaded here)"
MODEL="$HOME/.cache/chroma/onnx_models/all-MiniLM-L6-v2/onnx"
[ -f "$MODEL/model.onnx" ] || { echo "the search model is not cached at $MODEL - see THIRD_PARTY_NOTICES.md"; exit 1; }
mkdir -p "$R/models/all-MiniLM-L6-v2"
cp -R "$MODEL" "$R/models/all-MiniLM-L6-v2/onnx"

echo "== licences"
cp THIRD_PARTY_NOTICES.md "$R/"
"$R/python/bin/python3.12" desktop/macos/licences.py > "$R/BUNDLED_PACKAGES.md"

echo "== signing (ad hoc: no Developer ID on this Mac - a local test build, not notarized)"
find "$R" -type f \( -name '*.so' -o -name '*.dylib' \) -exec codesign --force --sign - --timestamp=none {} \;
for f in $(find "$R/python/bin" -type f -perm -u+x); do
    file "$f" | grep -q Mach-O && [ "$(basename "$f")" != python3.12 ] && codesign --force --sign - --timestamp=none "$f"
done
codesign --force --sign - --timestamp=none --options runtime \
    --entitlements desktop/macos/entitlements/python.entitlements "$R/python/bin/python3.12"
codesign --force --sign - --timestamp=none --options runtime \
    --entitlements desktop/macos/entitlements/helper.entitlements "$C/MacOS/ClinicServiceHelper"
codesign --force --sign - --timestamp=none --options runtime \
    --entitlements desktop/macos/entitlements/app.entitlements "$APP"
codesign --verify --strict --deep "$APP"

echo "== disk image"
STAGE="$WORK/dmg"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp desktop/macos/README-INSTALL.txt "$STAGE/READ ME FIRST.txt"
NAME="ClinicWorkspace-$VERSION-$BUILD-local"
rm -f "$OUT/$NAME.dmg"
hdiutil create -quiet -volname "Clinic Workspace $VERSION" -srcfolder "$STAGE" -format UDZO "$OUT/$NAME.dmg" \
    || { sleep 5; hdiutil create -quiet -volname "Clinic Workspace $VERSION" -srcfolder "$STAGE" -format UDZO "$OUT/$NAME.dmg"; }
(cd "$OUT" && shasum -a 256 "$NAME.dmg" > "$NAME.dmg.sha256")
cat > "$OUT/$NAME.build.json" <<EOF
{"version": "$VERSION", "build": $BUILD, "commit": "$COMMIT", "python": "$PYVER", "arch": "arm64",
 "minimum_macos": "14.0", "signing": "ad hoc (no Developer ID); not notarized",
 "sha256": "$(cut -d' ' -f1 "$OUT/$NAME.dmg.sha256")", "app_bytes": $(du -sk "$APP" | cut -f1)000}
EOF
echo "built $OUT/$NAME.dmg"
cat "$OUT/$NAME.dmg.sha256"
