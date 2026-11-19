#!/bin/bash
# The cloud preview (see MAC-MINI.md, "A preview of a cloud session"): serves the demo app of the
# branch with the newest commit on 127.0.0.1:8768, from a clone of its own, so work pushed from a cloud
# session (Claude Code on the web) can be looked at before it is merged. A cloud machine takes no
# connections from outside and its network carries no tunnel, so nothing comes from the session but
# its `git push`: this asks GitHub every 30 seconds and restarts the app when the newest commit
# changes. When every branch is merged, the newest commit is main's, and that is what it shows.
# Run by com.jflam.photo-preview.plist. Only the demo library is served.
export PATH="/opt/homebrew/bin:$PATH"
REPO="${PREVIEW_REPO:-https://github.com/jflam/photo-estate.git}"
DIR="${PREVIEW_DIR:-$HOME/Library/Caches/photo-estate-preview}"   # the clone: never edited by hand
PORT="${PREVIEW_PORT:-8768}"
BRANCH="${PREVIEW_BRANCH:-}"   # set to show one branch whatever is newer
EVERY=30

say() { echo "$(date '+%F %T') $*"; }
[ -d "$DIR/.git" ] || git clone --quiet "$REPO" "$DIR" || { say "could not clone $REPO"; exit 1; }
cd "$DIR" || exit 1

pid=
stop_app() {
    [ -n "$pid" ] || return 0
    kill "$pid" 2>/dev/null
    pkill -f "embed.py serve --demo --port $PORT" 2>/dev/null   # uv's child, should uv not pass the signal on
    wait "$pid" 2>/dev/null
    pid=
}
trap 'stop_app; exit 0' TERM INT

newest() {   # "<branch> <commit>" of the branch to show
    if [ -n "$BRANCH" ]; then
        git for-each-ref --format='%(refname:lstrip=3) %(objectname)' "refs/remotes/origin/$BRANCH"
    else
        git for-each-ref --sort=-committerdate --format='%(refname:lstrip=3) %(objectname)' refs/remotes/origin \
            | grep -v '^HEAD ' | head -n 1
    fi
}

shown=
while true; do
    git fetch --quiet --prune origin 2>/dev/null   # no network: carry on showing what is here
    set -- $(newest)
    branch="${1:-}" commit="${2:-}"
    if [ -n "$commit" ] && { [ "$commit" != "$shown" ] || ! kill -0 "$pid" 2>/dev/null; }; then
        stop_app
        if git checkout --quiet --force --detach "$commit"; then
            say "showing $branch at $(git log -1 --format='%h %s')"
            (cd photo-estate && HF_HUB_OFFLINE=1 exec uv run --quiet embed.py serve --demo --port "$PORT") &
            pid=$!
            shown="$commit"
        fi
    fi
    sleep "$EVERY" &
    wait $!   # a sleep that a TERM from launchd ends at once
done
