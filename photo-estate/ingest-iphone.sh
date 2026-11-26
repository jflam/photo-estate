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
# - The photo app runs this itself, with PHOTO_ESTATE_EVENTS=1, and reloads its catalog at the
#   catalog_ready event (core/events.py: `@@pe {json}` lines, printed only when asked). Run by hand, set
#   RESTART=com.jflam.photo-search to restart the app at the end instead.
# - With the catalog on another disk than the photos (the Mac mini: ~/estate on its SSD), say where each
#   is: DB=~/estate/estate.db STORE=/Volumes/estate/store INBOX=/Volumes/estate/iphone
#   CACHE=/Volumes/estate/review ./ingest-iphone.sh ~/estate   (the photo app passes all four itself).
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
CACHE=${CACHE:-}   # the app's thumbnail cache (--cache), if it is not ROOT/review: the face thumbnails go there

# The store is the disk that must be there; the catalog (ROOT) may be on another one, e.g. the internal SSD.
[[ -d $STORE ]] || { echo "store $STORE does not exist (is the disk mounted?)"; exit 1; }
[[ -d $INBOX ]] || { echo "nothing to do: $INBOX does not exist yet"; exit 0; }
LOCK=${TMPDIR:-/tmp}/photo-estate-ingest-iphone.lock
mkdir "$LOCK" 2>/dev/null || { echo "already running ($LOCK)"; exit 0; }
trap 'rmdir "$LOCK"' EXIT

run() { local s=$1; shift; echo "== $(date +%T) ${s%.py} $1"; uv run --quiet --script "$HERE/$s" --db "$DB" "$@"; }
count() { sqlite3 "$DB" "SELECT (SELECT count(*) FROM assets) + (SELECT count(*) FROM xmp)" 2>/dev/null || echo 0; }

# One line of core/events.py's protocol (version 1.0): event NAME [JSON_FIELDS], e.g. event new_assets '"count":3'.
event() { if [[ ${PHOTO_ESTATE_EVENTS:-} == 1 ]]; then printf '@@pe {"v":"1.0","type":"event","name":"%s"%s}\n' "$1" "${2:+,$2}"; fi; }
top() { sqlite3 "$DB" "SELECT coalesce(max(rowid), 0) FROM assets" 2>/dev/null || echo 0; }

before=$(count)
top_before=$(top)
echo "== $(date +%T) ingest $INBOX"
# --uploads: m<member id>/ says who shared each file, and a new photo is private to its sharer from the moment
# ingest records it, until `private.py check` below has scored it (private.py)
run estate.py ingest --store "$STORE" --force --uploads --facts "$INBOX.partial/facts" "$INBOX"   # facts: see upload.py
after=$(count)
event new_assets "\"count\":$((after - before))"
if [[ $before == "$after" ]]; then
  echo "nothing new"
  exit 0
fi
# exif, embed and faces look only at assets after the last run that finished (a few index lookups);
# asking "what has no embedding yet?" of the whole library takes ~10-20 s on a hard disk. A run that
# fails leaves the mark where it was, so the next run picks its photos up.
MARK=$INBOX.partial/catalogued-rowid
since=$(cat "$MARK" 2>/dev/null || echo "$top_before")
top_now=$(top)

echo "== $(date +%T) $((after - before)) new assets and sidecars: link, exif, places"
run estate.py link --source "$(basename "$INBOX")"
if command -v exiftool >/dev/null; then
  run estate.py exif --store "$STORE" --after-rowid "$since"
else
  echo "warning: exiftool not installed (brew install exiftool): skipping camera, lens and exposure"
fi
run places.py run
if [[ -z ${SKIP_MODELS:-} ]]; then run embed.py run --store "$STORE" --after-rowid "$since"; fi
# The hold at ingestion: each new phone upload with a vector (the app embeds uploads as they arrive, so even
# with SKIP_MODELS) is held as private to its sharer if it looks like a receipt, a screenshot or a document, and
# shared if not. Before catalog_ready, so the app's reload finds it decided; but it is private until then
# whatever the order (ingest wrote its row), and if this fails the run stops here and it stays private.
run private.py check
# The photo app reloads its catalog at this event: the new photos are searchable now, and faces (slow,
# and only the People page needs them) carry on while it does.
echo "== $(date +%T) catalog ready"
event catalog_ready
if [[ -z ${SKIP_MODELS:-} ]]; then
  run faces.py run --store "$STORE" --after-rowid "$since" ${CACHE:+--cache "$CACHE"}
  mkdir -p "$(dirname "$MARK")" && echo "$top_now" > "$MARK"   # everything up to here is catalogued
fi
if [[ -n ${RESTART:-} ]]; then
  echo "== $(date +%T) restart $RESTART"
  launchctl kickstart -k "gui/$(id -u)/$RESTART"
fi
echo "done $(date +%T)"
