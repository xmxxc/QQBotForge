#!/usr/bin/env bash
# Overlay CI test assets onto a source checkout.
#
# usage: stage-ci-assets.sh <source-dir> <assets-dir>
#
# The contents of <assets-dir>/tests are merged into <source-dir>/tests, so
# both end up under one test root whether or not the source already has a
# tests directory. A source test file is never replaced by different content.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: stage-ci-assets.sh <source-dir> <assets-dir>" >&2
  exit 2
fi
src="$1/tests"
assets="$2/tests"

if [ ! -d "$assets" ]; then
  echo "::error::test assets missing: $assets"
  exit 1
fi
mkdir -p "$src"
nested_before=0
if [ -e "$src/tests" ]; then
  nested_before=1
fi

conflicts=0
while IFS= read -r -d '' file; do
  rel="${file#"$assets"/}"
  if [ -e "$src/$rel" ] && ! cmp -s "$file" "$src/$rel"; then
    echo "::error::asset would overwrite source test: tests/$rel"
    conflicts=1
  fi
done < <(find "$assets" ! -type d -print0)
if [ "$conflicts" -ne 0 ]; then
  exit 1
fi

cp -R "$assets/." "$src/"

# Every asset file must sit at the same relative path under the source test root.
while IFS= read -r -d '' file; do
  rel="${file#"$assets"/}"
  if ! cmp -s "$file" "$src/$rel"; then
    echo "::error::asset not staged at tests/$rel"
    exit 1
  fi
done < <(find "$assets" ! -type d -print0)
if [ "$nested_before" -eq 0 ] && [ -e "$src/tests" ] && [ ! -e "$assets/tests" ]; then
  echo "::error::nested test root: $src/tests"
  exit 1
fi

echo "staged $(find "$assets" ! -type d | wc -l) asset files into $src ($(find "$src" ! -type d | wc -l) files total)"
