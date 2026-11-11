#!/usr/bin/env bash
# Mirror both iCloud Photos libraries onto the estate disk and ingest them into the store.
#
#   ./pull-icloud.sh /Volumes/estate
#
# - First sign in once (password + 2FA; the session then lasts about two months):
#     icloudpd --username "$ICLOUD_USER" --auth-only
# - iCloud is only read. Never add --delete-after-download or --keep-icloud-recent-days.
# - The mirror in ROOT/icloud/<library>/YYYY/MM/DD is kept: icloudpd uses it to know what it already
#   has, so re-running only fetches new photos. Files already in the store (most of them, from
#   Google) are de-duplicated by hash at ingest.
# - --adp-web-access is needed on this account: without it iCloud answers ACCESS_DENIED
#   ("private db access disabled"). The first run asks you to approve a prompt on an Apple device.
set -euo pipefail

ROOT=${1:?usage: pull-icloud.sh ESTATE_ROOT   (e.g. /Volumes/estate)}
: "${ICLOUD_USER:?set ICLOUD_USER to your Apple ID}"
LIBRARIES=${LIBRARIES:-"PrimarySync SharedSync-F64183F5-0137-4395-B0BE-927969E2FC13"}  # icloudpd --list-libraries
HERE=$(cd "$(dirname "$0")" && pwd)
DB=${DB:-$ROOT/estate.db}      # override to keep the catalog on a local disk when the store is on a NAS
STORE=${STORE:-$ROOT/store}

[[ -d $ROOT ]] || { echo "estate root $ROOT does not exist (is the disk mounted?)"; exit 1; }
estate() { uv run --quiet --script "$HERE/estate.py" --db "$DB" "$@"; }

for lib in $LIBRARIES; do
  echo "== $(date +%T) download $lib"
  icloudpd --username "$ICLOUD_USER" --adp-web-access --library "$lib" -d "$ROOT/icloud/$lib" \
    --size original --live-photo-size original --file-match-policy name-id7 --xmp-sidecar \
    --log-level info --no-progress-bar < /dev/null 2>&1 | { grep -v "Downloaded /" || true; }
done

for lib in $LIBRARIES; do   # --force: re-scan the mirror; files already catalogued are skipped
  echo "== $(date +%T) ingest $lib"
  estate ingest --store "$STORE" --force "$ROOT/icloud/$lib"
done

estate link
estate stats
sqlite3 "$DB" ".backup '$DB.bak-$(date +%Y%m%d)-icloud'"   # catalog snapshot
echo "done $(date +%T); review same-shot copies with: estate.py --db $DB dupes"
