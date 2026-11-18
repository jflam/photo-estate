#!/usr/bin/env bash
# fetch_round3.sh — a fresh set of pairs for the aesthetics judge (issue #44), from the photos round two scored but
# never showed, less everything the library has hidden since (the school photos). Run on the Mac after round two's
# data is here (fetch_round2.sh):
#   1. round3/ uses round two's sample.npz and teacher.csv (linked, not copied)
#   2. the catalog's hidden photos are read from the mini (read only) into round3/hidden.txt
#   3. round2_pairs.py --exclude hidden.txt, round two's photos, and any rejected.txt -> pairs.json + needed.txt
#      (refuses to change pairs.json if round3/judgments.jsonl exists)
#   4. rsync only the needed images into round3/img/ and verify each exists and opens
# Never touches a judgments.jsonl. Private photos: they stay on the mini and under that cache dir only.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE="${AESTHETICS_CACHE:-$HOME/Library/Caches/photo-estate-aesthetics}"
SRC="$CACHE/round2"
DEST="${ROUND3_DIR:-$CACHE/round3}"
REMOTE=johns-mac-mini
SSH_OPTS=(-o HostKeyAlias=johns-mac-mini -o HostName=100.100.132.37)
SEED=47

die() { echo "fetch_round3: $*" >&2; exit 1; }

[ -f "$SRC/sample.npz" ] && [ -f "$SRC/teacher.csv" ] && [ -f "$SRC/needed.txt" ] \
  || die "round two's data is not in $SRC; run fetch_round2.sh first"

# 1. the same scored sample
mkdir -p "$DEST/img"
ln -sf "$SRC/sample.npz" "$DEST/sample.npz"
ln -sf "$SRC/teacher.csv" "$DEST/teacher.csv"

# 2. what the library hides now
ssh "${SSH_OPTS[@]}" "$REMOTE" 'python3 -c "
import sqlite3
con = sqlite3.connect(\"file:/Volumes/estate/estate.db?mode=ro\", uri=True, timeout=60)
print(\"\\n\".join(sorted(r[0] for r in con.execute(\"SELECT DISTINCT sha256 FROM hidden\"))))"' > "$DEST/hidden.txt.tmp" \
  || die "cannot read the hidden photos from the mini; nothing changed"
[ -s "$DEST/hidden.txt.tmp" ] || die "the mini reports no hidden photos; that is not believable, stopping"
mv "$DEST/hidden.txt.tmp" "$DEST/hidden.txt"
echo "hidden in the library: $(wc -l < "$DEST/hidden.txt" | tr -d ' ') photos"

# 3. pairs: nothing hidden, nothing round two showed, nothing rejected while judging
EXCLUDE=(--exclude "$DEST/hidden.txt" --exclude "$SRC/needed.txt")
for f in "$CACHE"/*/rejected.txt; do
  [ -s "$f" ] && EXCLUDE+=(--exclude "$f")
done
"$HERE/round2_pairs.py" "$DEST" --seed "$SEED" "${EXCLUDE[@]}"

# 4. images: only the needed ones
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
echo "round three ready in $DEST: uv run $HERE/judge.py serve --dir $DEST --port 8773"
