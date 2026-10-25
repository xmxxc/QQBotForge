#!/usr/bin/env bash
# Overlay CI test assets onto a source checkout.
#
# usage: stage-ci-assets.sh <source-dir> <assets-dir>
#
# The contents of <assets-dir>/tests are merged into <source-dir>/tests, so
# both end up under one test root whether or not the source already has a
# tests directory. A source test entry is never replaced by different content
# or by an entry of another type (regular file, directory or symlink), and
# nothing is written outside <source-dir>.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: stage-ci-assets.sh <source-dir> <assets-dir>" >&2
  exit 2
fi
if [ -z "$1" ] || [ -z "$2" ]; then
  echo "::error::source-dir and assets-dir must not be empty" >&2
  exit 2
fi
if [ ! -d "$1" ]; then
  echo "::error::source-dir is not a directory: $1" >&2
  exit 2
fi
# Path-valued command substitutions end with a sentinel so trailing newlines
# that belong to the path are kept.
workspace=$(cd -- "$1" && pwd -P && echo x)
workspace=${workspace%$'\nx'}
src="$1/tests"
assets="$2/tests"

if [ -L "$assets" ]; then
  echo "::error::test assets root is a symlink: $assets"
  exit 1
fi
if [ ! -d "$assets" ]; then
  echo "::error::test assets missing: $assets"
  exit 1
fi

# The test root may only resolve to a directory strictly inside the source
# workspace; check before anything is created or copied.
if [ -L "$src" ]; then
  target=$(realpath -m -- "$src" && echo x)
  target=${target%$'\nx'}
  case "$target/" in
    "$workspace"/*/) ;;
    *)
      echo "::error::tests root $src is a symlink to $target, outside source workspace $workspace; refusing to write there"
      exit 1
      ;;
  esac
  if [ ! -d "$src" ]; then
    echo "::error::tests root $src is a symlink to $target, which is not an existing directory"
    exit 1
  fi
elif [ -e "$src" ] && [ ! -d "$src" ]; then
  echo "::error::tests root $src exists but is not a directory"
  exit 1
fi
mkdir -p "$src"
nested_before=0
if [ -e "$src/tests" ] || [ -L "$src/tests" ]; then
  nested_before=1
fi

# Type of a path without following a final symlink.
kind() {
  if [ -L "$1" ]; then
    echo symlink
  elif [ -d "$1" ]; then
    echo directory
  elif [ -f "$1" ]; then
    echo file
  elif [ -e "$1" ]; then
    echo other
  else
    echo missing
  fi
}

# Whether the source entry $2 already equals the asset entry $1 of type $3.
same_entry() {
  case "$3" in
    directory) return 0 ;;
    file) cmp -s -- "$1" "$2" ;;
    symlink) [ "$(readlink -- "$1" && echo x)" = "$(readlink -- "$2" && echo x)" ] ;;
    *) return 1 ;;
  esac
}

# find lists a directory before its contents, so once a directory conflicts
# its contents are skipped: their source paths would go through the conflict.
conflicts=0
blocked=""
while IFS= read -r -d '' file; do
  rel="${file#"$assets"/}"
  if [ -n "$blocked" ] && [ "${rel#"$blocked"/}" != "$rel" ]; then
    continue
  fi
  asset_kind=$(kind "$file")
  if [ "$asset_kind" = other ]; then
    echo "::error::unsupported asset type: tests/$rel"
    conflicts=1
    continue
  fi
  source_kind=$(kind "$src/$rel")
  if [ "$source_kind" = missing ]; then
    continue
  fi
  if [ "$source_kind" != "$asset_kind" ]; then
    echo "::error::asset type conflict at tests/$rel: source is $source_kind, asset is $asset_kind"
    conflicts=1
    if [ "$asset_kind" = directory ]; then
      blocked="$rel"
    fi
  elif ! same_entry "$file" "$src/$rel" "$asset_kind"; then
    echo "::error::asset would overwrite source test: tests/$rel"
    conflicts=1
  fi
done < <(find "$assets" -mindepth 1 -print0)
if [ "$conflicts" -ne 0 ]; then
  exit 1
fi

cp -R "$assets/." "$src/"

# Every asset entry must sit at the same relative path under the source test
# root with the same type and content.
while IFS= read -r -d '' file; do
  rel="${file#"$assets"/}"
  asset_kind=$(kind "$file")
  if [ "$(kind "$src/$rel")" != "$asset_kind" ] || ! same_entry "$file" "$src/$rel" "$asset_kind"; then
    echo "::error::asset not staged at tests/$rel"
    exit 1
  fi
done < <(find "$assets" -mindepth 1 -print0)
if [ "$nested_before" -eq 0 ] && [ -e "$src/tests" ] && [ ! -e "$assets/tests" ]; then
  echo "::error::nested test root: $src/tests"
  exit 1
fi

echo "staged $(find "$assets" ! -type d | wc -l) asset files into $src ($(find "$src" ! -type d | wc -l) files total)"
