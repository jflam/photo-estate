#!/bin/bash
# What the cloud preview's user can reach on the mini. Run by launchd as photopreview (a one-off job
# that preview-user-setup.sh starts), not with `sudo -u` from a terminal: a terminal's own disk
# permissions would be lent to the test. Every "must fail" line has to say DENIED.
# It reads nothing it should not: where a read succeeds the bytes go to /dev/null.
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
OWNER_HOME="${OWNER_HOME:-/Users/jflam}"
bad=0

must_fail() {   # must_fail <what> <command...>
    local what="$1" err; shift
    if err="$("$@" 2>&1 >/dev/null)"; then echo "EXPOSED  $what"; bad=1
    else echo "DENIED   $what  ($(printf '%s' "$err" | tail -n 1))"; fi
}
must_work() {   # a control: shows the job itself runs, so a DENIED above is a real refusal
    local what="$1" err; shift
    if err="$("$@" 2>&1 >/dev/null)"; then echo "OK       $what"
    else echo "BROKEN   $what  ($(printf '%s' "$err" | tail -n 1))"; bad=1; fi
}

echo "isolation check, $(date '+%F %T'), as $(id -un) (uid $(id -u)), groups: $(id -Gn)"
echo "-- controls (must work)"
must_work "write in its own home"               touch "$HOME/.isolation-check"
must_work "read preview-branch.sh"              head -c 1 /usr/local/photo-preview/preview-branch.sh
must_work "run uv's python"                     uv run --no-project --quiet python -c "pass"
echo "-- must fail"
must_fail "write preview-branch.sh"             sh -c ': >> /usr/local/photo-preview/preview-branch.sh'
must_fail "list /Volumes/estate"                ls /Volumes/estate
must_fail "read /Volumes/estate/estate.db"      head -c 16 /Volumes/estate/estate.db
must_fail "list /Volumes/estate with uv's python" \
    uv run --no-project --quiet python -c "import os; os.listdir('/Volumes/estate')"
must_fail "list $OWNER_HOME"                    ls "$OWNER_HOME"
must_fail "list $OWNER_HOME/.ssh"               ls "$OWNER_HOME/.ssh"
must_fail "read $OWNER_HOME/.ssh/known_hosts"   cat "$OWNER_HOME/.ssh/known_hosts"
must_fail "list $OWNER_HOME/Documents"          ls "$OWNER_HOME/Documents"
must_fail "read $OWNER_HOME/Documents/.localized" cat "$OWNER_HOME/Documents/.localized"
must_fail "read $OWNER_HOME/.config/photo-estate/agent.env" cat "$OWNER_HOME/.config/photo-estate/agent.env"
echo "-- must fail (preview-firewall-setup.sh): this machine's own services, and the networks it is on"
must_fail "connect to the real app, 127.0.0.1:8766"   curl -s -m 5 -o /dev/null http://127.0.0.1:8766/api/info
must_fail "connect to the demo, 127.0.0.1:8767"       curl -s -m 5 -o /dev/null http://127.0.0.1:8767/api/info
must_fail "connect to localhost:8766 (IPv6 too)"      curl -s -m 5 -o /dev/null http://localhost:8766/api/info
for addr in $(ifconfig | awk '$1 == "inet" && $2 != "127.0.0.1" {print $2}'); do   # LAN and tailnet addresses of this machine
    must_fail "connect to this machine at $addr:22"   nc -z -G 3 "$addr" 22
    must_fail "connect to the real app at $addr:8766" nc -z -G 3 "$addr" 8766
done
gw="$(route -n get default 2>/dev/null | awk '$1 == "gateway:" {print $2}')"
[ -z "$gw" ] || must_fail "connect to the router, $gw:53"  nc -z -G 3 "$gw" 53
echo "-- controls (must work): the preview's own needs"
must_work "fetch from GitHub"                         git ls-remote https://github.com/jflam/photo-estate.git HEAD
must_work "reach the package index"                   curl -s -m 10 -o /dev/null https://pypi.org/simple/pillow/
echo "INFO     its own app, http://127.0.0.1:8768/api/info, answers $(curl -s -o /dev/null -m 5 -w '%{http_code}' http://127.0.0.1:8768/api/info)"
[ $bad -eq 0 ] && echo "RESULT: every must-fail was denied" || echo "RESULT: NOT ISOLATED, see EXPOSED/BROKEN above"
