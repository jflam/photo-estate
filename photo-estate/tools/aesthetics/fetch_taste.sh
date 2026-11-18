#!/usr/bin/env bash
# fetch_taste.sh — train the taste scorer on the judged rounds (train_taste.py) and bring from the mini the images
# its page shows. Idempotent. Private photos: they stay on the mini and under the cache dir only.
#   ./fetch_taste.sh [train_taste.py options]   then open <cache>/taste/index.html
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE="${AESTHETICS_CACHE:-$HOME/Library/Caches/photo-estate-aesthetics}"
REMOTE=johns-mac-mini
SSH_OPTS=(-o HostKeyAlias=johns-mac-mini -o HostName=100.100.132.37)

"$HERE/train_taste.py" --cache "$CACHE" "$@"
rsync -t --partial -e "ssh ${SSH_OPTS[*]}" \
  --files-from="$CACHE/taste/needed.txt" "$REMOTE:aesthetic-20k/img/" "$CACHE/taste/img/"
missing=0
while read -r n; do [ -s "$CACHE/taste/img/$n" ] || { echo "MISSING: $n" >&2; missing=1; }; done < "$CACHE/taste/needed.txt"
[ "$missing" = 0 ] || exit 1
echo "taste page ready: $CACHE/taste/index.html"
