#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
INSTALL_ROOT=${1:-"$HOME/.local/lib/orca-adaptive-routing"}
BIN_ROOT=${2:-"$HOME/.local/bin"}

if ! git -C "$SOURCE_ROOT" diff --quiet || ! git -C "$SOURCE_ROOT" diff --cached --quiet; then
  echo "Refusing to install from a dirty source tree." >&2
  exit 1
fi

COMMIT=$(git -C "$SOURCE_ROOT" rev-parse HEAD)
VERSION=$(python3 -c 'import tomllib,sys; print(tomllib.load(open(sys.argv[1],"rb"))["project"]["version"])' "$SOURCE_ROOT/pyproject.toml")
TARGET="$INSTALL_ROOT/$COMMIT"
STAGING=$(mktemp -d "${TMPDIR:-/tmp}/orca-adaptive-install.XXXXXX")
trap 'rm -rf "$STAGING"' EXIT

mkdir -p "$STAGING/package" "$TARGET" "$BIN_ROOT"
cp -a "$SOURCE_ROOT/adaptive_coordinator" "$STAGING/package/"
find "$STAGING/package" -type d -name __pycache__ -prune -exec rm -rf {} +
printf '%s\n' "$COMMIT" > "$STAGING/package/INSTALL_COMMIT"
printf '%s\n' "$VERSION" > "$STAGING/package/INSTALL_VERSION"

rm -rf "$TARGET"
mv "$STAGING/package" "$TARGET"

LAUNCHER="$TARGET/orca-adaptive"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH='$TARGET'
exec python3 -c 'from adaptive_coordinator.cli import main; raise SystemExit(main())' "\$@"
EOF
chmod 0755 "$LAUNCHER"
ln -sfn "$LAUNCHER" "$BIN_ROOT/orca-adaptive"

printf 'installed_version=%s\ninstalled_commit=%s\ncommand=%s\n' \
  "$VERSION" "$COMMIT" "$BIN_ROOT/orca-adaptive"
