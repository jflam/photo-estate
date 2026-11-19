#!/usr/bin/env bash
# Shows the web app of this checkout, on the demo library, to a person: for a look at work in a cloud
# session (Claude Code on the web) before it is merged. A cloud session takes no connections from
# outside and its network carries no tunnel, so the work goes out the one way it can, as a pushed
# branch: the Mac mini serves the demo app of the branch with the newest commit on its port 8768, on
# the tailnet (deploy/preview-branch.sh; see deploy/MAC-MINI.md). The preview stays up after the
# session's machine has stopped.
#
#   bash photo-estate/tools/cloud-preview.sh     push this branch, and say what the mini will show
#
# It commits nothing: what is not committed is not in the preview, so it stops and says so.
set -u

cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1
branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
case "$branch" in
    "" | HEAD) echo "PREVIEW NOT PUSHED: this checkout is on no branch (git switch -c <name>)"; exit 1 ;;
    main) echo "PREVIEW NOT PUSHED: this is main; commit the work to a new branch (git switch -c <name>)"; exit 1 ;;
esac
if [ -n "$(git status --porcelain)" ]; then
    echo "PREVIEW NOT PUSHED: these changes are not committed, and the mini shows only what is pushed:"
    git status --short | head -n 20
    exit 1
fi

pushed=1
for wait in 0 2 4 8 16; do
    sleep "$wait"
    if out="$(git push -u origin "$branch" 2>&1)"; then pushed=0; break; fi
done
if [ $pushed -ne 0 ] || [ "$(git ls-remote origin "refs/heads/$branch" | cut -f1)" != "$(git rev-parse HEAD)" ]; then
    echo "PREVIEW NOT PUSHED: \`git push origin $branch\` failed:"
    printf '%s\n' "$out" | tail -n 10
    exit 1
fi
echo "PREVIEW PUSHED: $branch at $(git log -1 --format='%h %s')"
echo "Open: port 8768 of the Mac mini, on the tailnet (https://johns-mac-mini.<tailnet>.ts.net:8768)"
echo "The mini shows the branch with the newest commit, so this one until something newer is pushed."
echo "It looks every 30 seconds: allow a minute. It stays up when this session ends."
