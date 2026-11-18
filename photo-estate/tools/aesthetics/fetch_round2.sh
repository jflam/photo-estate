#!/usr/bin/env bash
# fetch_round2.sh — the whole data step for aesthetics round two (issue #44), idempotent. Run on the Mac once the
# mini's ~/aesthetic-20k/DONE exists:
#   1. refuse unless DONE exists and FAILED does not
#   2. rsync sample.npz + teacher.csv from the mini (read only there) into ~/Library/Caches/photo-estate-aesthetics/round2/
#   3. check teacher.csv: 20,000 unique shas, all in sample.npz (shortfalls are reported loudly, not hidden)
#   4. round2_pairs.py -> pairs.json + needed.txt (refuses to change pairs.json if judgments.jsonl exists)
#   5. rsync only the needed images into round2/img/ and verify each exists and opens
# Never touches round2/judgments.jsonl. Private photos: they stay on the mini and under that cache dir only.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${ROUND2_DIR:-$HOME/Library/Caches/photo-estate-aesthetics/round2}"
REMOTE=johns-mac-mini
SSH_OPTS=(-o HostKeyAlias=johns-mac-mini -o HostName=100.100.132.37)
EXPECT=20000

die() { echo "fetch_round2: $*" >&2; exit 1; }

# 1. is the scoring job finished?
state="$(ssh "${SSH_OPTS[@]}" "$REMOTE" 'cd ~/aesthetic-20k 2>/dev/null || { echo NODIR; exit 0; }
  if [ -e FAILED ]; then echo FAILED; elif [ -e DONE ]; then echo DONE; else echo PENDING; fi')" \
  || die "cannot reach the mini over ssh; nothing fetched"
case "$state" in
  DONE) echo "mini: ~/aesthetic-20k/DONE exists" ;;
  FAILED) die "the mini reports FAILED (~/aesthetic-20k/FAILED exists); not fetching. Look at ~/aesthetic-20k/teacher.out there." ;;
  PENDING) die "~/aesthetic-20k/DONE does not exist yet: the teacher is still scoring. Run this again once it appears." ;;
  *) die "~/aesthetic-20k does not exist on the mini ($state)" ;;
esac

# 2. scores and embeddings
mkdir -p "$DEST/img"
rsync -t --partial -e "ssh ${SSH_OPTS[*]}" \
  "$REMOTE:aesthetic-20k/sample.npz" "$REMOTE:aesthetic-20k/teacher.csv" "$DEST/"

# 3. sanity: 20,000 unique scored shas, all present in sample.npz
uv run --quiet --with numpy python - "$DEST" "$EXPECT" <<'PY' || echo "WARNING: teacher.csv / sample.npz check reported a shortfall (above); continuing, but read it" >&2
import csv, re, sys
import numpy as np
d, expect = sys.argv[1], int(sys.argv[2])
rows = [r for r in csv.reader(open(f"{d}/teacher.csv", newline="")) if r and r[0] != "sha"]
good = [r for r in rows if len(r) == 2 and re.fullmatch(r"[0-9a-f]{64}", r[0])]
uniq = {r[0] for r in good}
sample = set(str(s) for s in np.load(f"{d}/sample.npz")["sha"])
print(f"teacher.csv: {len(rows)} rows, {len(good)} well-formed, {len(uniq)} unique shas; sample.npz: {len(sample)} photos")
bad = False
if len(rows) != len(good):
    print(f"  SHORTFALL: {len(rows) - len(good)} malformed rows"); bad = True
if len(good) != len(uniq):
    print(f"  SHORTFALL: {len(good) - len(uniq)} duplicate rows"); bad = True
if len(uniq) != expect:
    print(f"  SHORTFALL: {len(uniq)} unique scored shas, expected {expect}"); bad = True
if uniq - sample:
    print(f"  SHORTFALL: {len(uniq - sample)} scored shas are not in sample.npz"); bad = True
if sample - uniq:
    print(f"  SHORTFALL: {len(sample - uniq)} sample photos have no teacher score"); bad = True
sys.exit(1 if bad else 0)
PY

# 4. pairs (refuses itself if it would change pairs.json under existing judgments)
"$HERE/round2_pairs.py" "$DEST"

# 5. images: only the needed ones
rsync -t --partial -e "ssh ${SSH_OPTS[*]}" \
  --files-from="$DEST/needed.txt" "$REMOTE:aesthetic-20k/img/" "$DEST/img/"

uv run --quiet --with pillow python - "$DEST" <<'PY'
import sys
from pathlib import Path
from PIL import Image
d = Path(sys.argv[1])
names = [l.strip() for l in (d / "needed.txt").read_text().splitlines() if l.strip()]
bad, total = [], 0
for n in names:
    p = d / "img" / n
    try:
        total += p.stat().st_size
        with Image.open(p) as im:
            im.verify()
    except Exception as e:
        bad.append(f"{n}: {e}")
print(f"images: {len(names)} needed, {len(names) - len(bad)} present and open, {total / 1e6:.1f} MB")
if bad:
    print("MISSING OR UNREADABLE:\n  " + "\n  ".join(bad[:20]))
    sys.exit(1)
PY
echo "round two ready in $DEST (judgments.jsonl untouched)"
