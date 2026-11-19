#!/bin/bash
# The mini's end of the cloud preview (see MAC-MINI.md, "A preview of a cloud session"): keeps
# 127.0.0.1:8768 forwarded, through tailcat, to the demo app a cloud session is serving on its port
# 8767. `tailcat forward` does not find the server again after the server has restarted, and a cloud
# session comes and goes, so this asks for a page through the forward every few seconds and starts
# the forward again when two in a row get no answer. Run by com.jflam.photo-preview-relay.plist.
export PATH=/opt/homebrew/bin:/usr/bin:/bin
ADDR_FILE="${PREVIEW_ADDR_FILE:-$HOME/.config/photo-estate/preview.addr}"   # the server key's tailcat address: a secret
LOCAL=8768
REMOTE=8767

addr="$(cat "$ADDR_FILE" 2>/dev/null)"
[ -n "$addr" ] || { echo "no tailcat address in $ADDR_FILE"; exit 1; }
pid=
stop_forward() {   # a forward that has lost its server does not always end when asked to: then it is made to
    [ -n "$pid" ] || return 0
    kill "$pid" 2>/dev/null
    for _ in 1 2 3; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -9 "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
    pid=
}
trap 'stop_forward; exit 0' TERM INT

up=0
while true; do
    tailcat forward "$addr" "$LOCAL:$REMOTE" >/dev/null 2>&1 &
    pid=$!
    sleep 3
    fails=0
    while kill -0 "$pid" 2>/dev/null && [ "$fails" -lt 2 ]; do
        if curl -s -m 10 -o /dev/null "http://127.0.0.1:$LOCAL/api/info"; then
            [ "$up" = 1 ] || echo "$(date '+%F %T') a cloud session is being previewed"
            up=1 fails=0
        else
            fails=$((fails + 1))
        fi
        sleep 10
    done
    [ "$up" = 0 ] || echo "$(date '+%F %T') the cloud session has gone"
    up=0
    stop_forward
done
