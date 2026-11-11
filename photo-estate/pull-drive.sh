#!/usr/bin/env bash
# Pull the Google Photos Takeout zips from Google Drive into the estate, one zip at a time.
#
#   ./pull-drive.sh /Volumes/estate            # estate root on the external disk
#
# - Zips are staged on the internal SSD (STAGING, default ~/estate-staging) so the HDD only sees
#   sequential writes into the store.
# - The next zip downloads while the current one is ingested (needs ~2 x 54 GB of staging space).
# - Each zip is deleted from staging once it is ingested; Drive is never modified (read-only remote).
# - Safe to stop and re-run: zips already ingested are skipped without downloading, and an
#   interrupted ingest resumes where it left off.
set -euo pipefail

ROOT=${1:?usage: pull-drive.sh ESTATE_ROOT   (e.g. /Volumes/estate)}
REMOTE=${REMOTE:-gdrive:Takeout}
PATTERN=${PATTERN:-takeout-20260523T191842Z-3-*.zip}
STAGING=${STAGING:-$HOME/estate-staging}
HERE=$(cd "$(dirname "$0")" && pwd)
DB=${DB:-$ROOT/estate.db}      # override to keep the catalog on a local disk when the store is on a NAS
STORE=${STORE:-$ROOT/store}

[[ -d $ROOT ]] || { echo "estate root $ROOT does not exist (is the disk mounted?)"; exit 1; }
mkdir -p "$STORE" "$STAGING"

estate() { uv run --quiet --script "$HERE/estate.py" --db "$DB" "$@"; }

ingested() {  # is this zip already fully ingested?
  [[ -f $DB ]] && [[ -n $(sqlite3 "$DB" \
    "SELECT 1 FROM sources WHERE name='$1' AND finished_at IS NOT NULL" 2>/dev/null) ]]
}

fetch() {  # download one zip into staging; rclone checks size + MD5 against Drive
  local name=$1 need=$2 free
  if [[ -f $STAGING/$name ]] && [[ $(wc -c < "$STAGING/$name" | tr -d " ") == "$need" ]]; then return; fi
  # btrfs frees a deleted 50 GB zip's space lazily, so wait up to 15 min for it to show up
  for _ in $(seq 90); do
    free=$(df --output=avail -B1 "$STAGING" | tail -1)
    (( free >= need + 5*1024**3 )) && break
    sync -f "$STAGING"; sleep 10
  done
  if (( free < need + 5*1024**3 )); then
    echo "not enough space in $STAGING for $name ($((need/1024**3)) GiB needed)"; return 1
  fi
  rclone copy "$REMOTE/$name" "$STAGING/" --multi-thread-streams 8 --stats=60s --stats-one-line --stats-log-level NOTICE 2>&1 |
    { grep -v "shared Google Drive client_id" || true; }   # show progress, hide the repeated key warning
}

mapfile -t ZIPS < <(rclone lsjson "$REMOTE" --files-only --include "$PATTERN" 2>/dev/null |
  python3 -c 'import json,sys; [print(f["Name"], f["Size"]) for f in sorted(json.load(sys.stdin), key=lambda f: f["Name"])]')
(( ${#ZIPS[@]} )) || { echo "no zips matching $PATTERN in $REMOTE"; exit 1; }

todo=()
for z in "${ZIPS[@]}"; do
  read -r name size <<<"$z"
  if ingested "$name"; then echo "skip $name (already ingested)"; else todo+=("$z"); fi
done
echo "${#todo[@]} of ${#ZIPS[@]} zips to pull into $ROOT"

prefetch_pid=
for i in "${!todo[@]}"; do
  read -r name size <<<"${todo[$i]}"
  if [[ -n $prefetch_pid ]]; then   # a failed prefetch isn't fatal: fetch below retries it
    wait "$prefetch_pid" || echo "  background download of $name failed; retrying in the foreground"
    prefetch_pid=
  fi
  echo "== $(date +%T) [$((i+1))/${#todo[@]}] $name ($((size/1024**3)) GiB)"
  fetch "$name" "$size"
  if (( i + 1 < ${#todo[@]} )); then   # start downloading the next zip in the background
    read -r nname nsize <<<"${todo[$((i+1))]}"
    fetch "$nname" "$nsize" & prefetch_pid=$!
  fi
  estate ingest --store "$STORE" --delete-after "$STAGING/$name"
done
if [[ -n $prefetch_pid ]]; then wait "$prefetch_pid" || true; fi

estate link
estate stats
sqlite3 "$DB" ".backup '$DB.bak-$(date +%Y%m%d)'"   # catalog snapshot
echo "done $(date +%T)"
