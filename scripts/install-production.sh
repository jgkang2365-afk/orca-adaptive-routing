#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
INSTALL_ROOT=${1:-"$HOME/.local/lib/orca-adaptive-routing"}
BIN_ROOT=${2:-"$HOME/.local/bin"}

if [[ -n "$(git -C "$SOURCE_ROOT" status --porcelain=v1 --untracked-files=all)" ]]; then
  echo "Refusing to install from a dirty source tree." >&2
  exit 1
fi

COMMIT=$(git -C "$SOURCE_ROOT" rev-parse HEAD)
TARGET="$INSTALL_ROOT/$COMMIT"
STAGING=$(mktemp -d "${TMPDIR:-/tmp}/orca-adaptive-install.XXXXXX")
trap 'rm -rf "$STAGING"' EXIT

mkdir -p "$STAGING/snapshot" "$STAGING/package" "$INSTALL_ROOT" "$BIN_ROOT"
git -C "$SOURCE_ROOT" archive --format=tar "$COMMIT" \
  adaptive_coordinator pyproject.toml |
  tar -xf - -C "$STAGING/snapshot"
VERSION=$(python3 -c 'import tomllib,sys; print(tomllib.load(open(sys.argv[1],"rb"))["project"]["version"])' "$STAGING/snapshot/pyproject.toml")
mv "$STAGING/snapshot/adaptive_coordinator" "$STAGING/package/"
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
