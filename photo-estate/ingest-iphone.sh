#!/usr/bin/env bash
# Catalogue whatever the phone has uploaded since the last run.
#
#   ./ingest-iphone.sh /Volumes/estate          (what a launchd timer runs)
#
# The photo app drops uploads in ROOT/iphone (see upload.py). This ingests that folder, and only if
# it brought anything new runs the rest of the pipeline on the new photos: every stage below only
# touches what is new: link is limited to the inbox, exif/embed/faces skip finished photos, and places
# reuses the place names and zones it already worked out (events and trips are rebuilt, in seconds). Nothing is deleted.
#
# - Set RESTART=com.jflam.photo-search to restart the photo app at the end: it reads the catalog at
#   startup, so new photos appear in search only after that.
# - Set SKIP_MODELS=1 to catalogue only (ingest, link, exif, places) and leave embeddings and faces
#   for later, e.g. while the Mac is busy.
# - Safe to run while the photo app is up (the catalog is in WAL mode), and safe to overlap: a second
#   run exits at once if one is already going.
set -euo pipefail

ROOT=${1:?usage: ingest-iphone.sh ESTATE_ROOT   (e.g. /Volumes/estate)}
HERE=$(cd "$(dirname "$0")" && pwd)
DB=${DB:-$ROOT/estate.db}
STORE=${STORE:-$ROOT/store}
INBOX=${INBOX:-$ROOT/iphone}

[[ -d $INBOX ]] || { echo "nothing to do: $INBOX does not exist yet"; exit 0; }
[[ -d $ROOT ]] || { echo "estate root $ROOT does not exist (is the disk mounted?)"; exit 1; }
LOCK=${TMPDIR:-/tmp}/photo-estate-ingest-iphone.lock
mkdir "$LOCK" 2>/dev/null || { echo "already running ($LOCK)"; exit 0; }
trap 'rmdir "$LOCK"' EXIT

run() { local s=$1; shift; uv run --quiet --script "$HERE/$s" --db "$DB" "$@"; }
count() { sqlite3 "$DB" "SELECT (SELECT count(*) FROM assets) + (SELECT count(*) FROM xmp)" 2>/dev/null || echo 0; }

before=$(count)
echo "== $(date +%T) ingest $INBOX"
run estate.py ingest --store "$STORE" --force "$INBOX"
after=$(count)
if [[ $before == "$after" ]]; then
  echo "nothing new"
  exit 0
fi

echo "== $(date +%T) $((after - before)) new assets and sidecars: link, exif, places"
run estate.py link --source "$(basename "$INBOX")"
if command -v exiftool >/dev/null; then
  run estate.py exif --store "$STORE"
else
  echo "warning: exiftool not installed (brew install exiftool): skipping camera, lens and exposure"
fi
run places.py run
if [[ -z ${SKIP_MODELS:-} ]]; then
  echo "== $(date +%T) embeddings, faces"
  run embed.py run --store "$STORE"
  run faces.py run --store "$STORE"
fi
if [[ -n ${RESTART:-} ]]; then
  echo "== $(date +%T) restart $RESTART"
  launchctl kickstart -k "gui/$(id -u)/$RESTART"
fi
echo "done $(date +%T)"
