#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
INSTALL_ROOT=${1:-"$HOME/.local/lib/orca-adaptive-routing"}
BIN_ROOT=${2:-"$HOME/.local/bin"}
SKILL_ROOT=${3:-"$HOME/.agents/skills"}
CODEX_SKILL_ROOT=${4:-"${CODEX_HOME:-$HOME/.codex}/skills"}
if (( $# >= 5 )); then
  ORCA_MANAGED_SKILL_ROOT=$5
elif [[ -d "$HOME/.local/share/orca/codex-runtime-home/home" ]]; then
  ORCA_MANAGED_SKILL_ROOT="$HOME/.local/share/orca/codex-runtime-home/home/skills"
else
  ORCA_MANAGED_SKILL_ROOT=""
fi

if [[ -n "$(git -C "$SOURCE_ROOT" status --porcelain=v1 --untracked-files=all)" ]]; then
  echo "Refusing to install from a dirty source tree." >&2
  exit 1
fi

COMMIT=$(git -C "$SOURCE_ROOT" rev-parse HEAD)
TARGET="$INSTALL_ROOT/$COMMIT"
SHARED_SKILL_LINK="$SKILL_ROOT/orca-adaptive-routing"
CODEX_SKILL_LINK="$CODEX_SKILL_ROOT/orca-adaptive-routing"
SKILL_LINKS=("$SHARED_SKILL_LINK")
if [[ "$CODEX_SKILL_LINK" != "$SHARED_SKILL_LINK" ]]; then
  SKILL_LINKS+=("$CODEX_SKILL_LINK")
fi
ORCA_MANAGED_SKILL_LINK=""
if [[ -n "$ORCA_MANAGED_SKILL_ROOT" ]]; then
  ORCA_MANAGED_SKILL_LINK="$ORCA_MANAGED_SKILL_ROOT/orca-adaptive-routing"
  if [[ "$ORCA_MANAGED_SKILL_LINK" != "$SHARED_SKILL_LINK" && \
        "$ORCA_MANAGED_SKILL_LINK" != "$CODEX_SKILL_LINK" ]]; then
    SKILL_LINKS+=("$ORCA_MANAGED_SKILL_LINK")
  fi
fi
MIGRATE_SKILLS=()
for SKILL_LINK in "${SKILL_LINKS[@]}"; do
  if [[ -L "$SKILL_LINK" ]]; then
    RAW_SKILL_TARGET=$(readlink "$SKILL_LINK")
    RESOLVED_SKILL_TARGET=$(readlink -f "$SKILL_LINK" || true)
    MANAGED_SKILL_LINK=false
    if [[ "$RAW_SKILL_TARGET" == "$SHARED_SKILL_LINK" && "$SKILL_LINK" != "$SHARED_SKILL_LINK" ]]; then
      MANAGED_SKILL_LINK=true
    elif [[ "$RESOLVED_SKILL_TARGET" == "$INSTALL_ROOT"/*/orca-adaptive-routing-skill ]]; then
      MANAGED_SKILL_LINK=true
    fi
    if [[ "$MANAGED_SKILL_LINK" != true ]]; then
      echo "Refusing to replace an unmanaged skill symlink: $SKILL_LINK" >&2
      exit 1
    fi
  elif [[ -e "$SKILL_LINK" ]]; then
    LEGACY_SKILL="$SKILL_LINK.pre-snapshot"
    if [[ ! -f "$SKILL_LINK/SKILL.md" ]] || \
       ! grep -qx 'name: orca-adaptive-routing' "$SKILL_LINK/SKILL.md" || \
       [[ -e "$LEGACY_SKILL" ]]; then
      echo "Refusing to replace an unmanaged or already-backed-up skill installation: $SKILL_LINK" >&2
      exit 1
    fi
    MIGRATE_SKILLS+=("$SKILL_LINK")
  fi
done
STAGING=$(mktemp -d "${TMPDIR:-/tmp}/orca-adaptive-install.XXXXXX")
trap 'rm -rf "$STAGING"' EXIT

mkdir -p "$STAGING/snapshot" "$STAGING/package" "$INSTALL_ROOT" "$BIN_ROOT" \
  "$SKILL_ROOT" "$CODEX_SKILL_ROOT"
if [[ -n "$ORCA_MANAGED_SKILL_ROOT" ]]; then
  mkdir -p "$ORCA_MANAGED_SKILL_ROOT"
fi
git -C "$SOURCE_ROOT" archive --format=tar "$COMMIT" \
  adaptive_coordinator pyproject.toml skills/orca-adaptive-routing |
  tar -xf - -C "$STAGING/snapshot"
VERSION=$(python3 -c 'import tomllib,sys; print(tomllib.load(open(sys.argv[1],"rb"))["project"]["version"])' "$STAGING/snapshot/pyproject.toml")
mv "$STAGING/snapshot/adaptive_coordinator" "$STAGING/package/"
mv "$STAGING/snapshot/skills/orca-adaptive-routing" "$STAGING/package/orca-adaptive-routing-skill"
printf '%s\n' "$COMMIT" > "$STAGING/package/INSTALL_COMMIT"
printf '%s\n' "$VERSION" > "$STAGING/package/INSTALL_VERSION"

rm -rf "$TARGET"
mv "$STAGING/package" "$TARGET"

LAUNCHER="$TARGET/orca-adaptive"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec python3 -I -c 'import sys; target=sys.argv.pop(1); sys.path.insert(0,target); from adaptive_coordinator.cli import main; raise SystemExit(main())' '$TARGET' "\$@"
EOF
chmod 0755 "$LAUNCHER"
ln -sfn "$LAUNCHER" "$BIN_ROOT/orca-adaptive"
for SKILL_LINK in "${MIGRATE_SKILLS[@]}"; do
  mv "$SKILL_LINK" "$SKILL_LINK.pre-snapshot"
done
for SKILL_LINK in "${SKILL_LINKS[@]}"; do
  ln -sfn "$TARGET/orca-adaptive-routing-skill" "$SKILL_LINK"
done

printf 'installed_version=%s\ninstalled_commit=%s\ncommand=%s\nshared_skill=%s\ncodex_skill=%s\norca_managed_skill=%s\n' \
  "$VERSION" "$COMMIT" "$BIN_ROOT/orca-adaptive" "$SHARED_SKILL_LINK" \
  "$CODEX_SKILL_LINK" "$ORCA_MANAGED_SKILL_LINK"
