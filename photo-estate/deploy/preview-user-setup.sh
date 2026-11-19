#!/bin/bash
# Moves the cloud preview (MAC-MINI.md, "A preview of a cloud session") from a launchd agent of the
# library's owner to a LaunchDaemon that runs as its own user, photopreview: a hidden, standard user
# with no password, outside the staff and admin groups. The preview runs whatever is pushed to any
# branch of the repository, so it should not run as someone who can read the library.
#
#   sudo bash preview-user-setup.sh      (needs root; safe to run again; prints each step)
#
# Beside this script it needs preview-branch.sh, preview-isolation-check.sh and
# com.jflam.photo-preview.plist (or the three in a folder named photo-preview-files).
# It does not touch the real app (8766), the demo (8767), ~/src/photo-estate or /Volumes/estate.
set -euo pipefail

U=photopreview                          # the new user, and its group
OWNER="${SUDO_USER:-jflam}"             # whose agent is replaced and whose model is copied
LABEL=com.jflam.photo-preview
PORT=8768
LIB=/usr/local/photo-preview            # root's: the preview can read it, not change it
LOGDIR=/var/log/photo-preview           # the preview's: it writes here, anyone may read
HOME_DIR=/Users/$U
MODEL=models--google--siglip2-base-patch16-256
DAEMON=/Library/LaunchDaemons/$LABEL.plist

step() { printf '\n== %s\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run this with sudo"; exit 1; }
[ "$OWNER" != root ] || { echo "run this with sudo from the owner's account, not as root"; exit 1; }
OWNER_UID="$(id -u "$OWNER")"
OWNER_HOME="$(dscl . -read "/Users/$OWNER" NFSHomeDirectory | awk '{print $2}')"
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE"; [ -f "$SRC/preview-branch.sh" ] || SRC="$HERE/photo-preview-files"
for f in preview-branch.sh preview-isolation-check.sh $LABEL.plist; do
    [ -f "$SRC/$f" ] || { echo "missing $f beside this script"; exit 1; }
done

step "1. the user $U: hidden, standard, no password, a group of its own"
if dscl . -read "/Users/$U" UniqueID >/dev/null 2>&1; then
    ID="$(dscl . -read "/Users/$U" UniqueID | awk '{print $2}')"
    echo "already there, uid $ID"
else
    ID=600   # the first number from 600 that is no user's and no group's
    while dscl . -list /Users UniqueID | awk '{print $2}' | grep -qx "$ID" ||
          dscl . -list /Groups PrimaryGroupID | awk '{print $2}' | grep -qx "$ID"; do ID=$((ID + 1)); done
    dscl . -create "/Groups/$U"
    dscl . -create "/Groups/$U" PrimaryGroupID "$ID"
    dscl . -create "/Groups/$U" RealName "Photo Estate preview"
    dscl . -create "/Groups/$U" Password "*"
    dscl . -create "/Users/$U"
    dscl . -create "/Users/$U" UniqueID "$ID"
    dscl . -create "/Users/$U" PrimaryGroupID "$ID"      # its own group, NOT staff (20): staff can read ~$OWNER
    dscl . -create "/Users/$U" RealName "Photo Estate preview"
    dscl . -create "/Users/$U" NFSHomeDirectory "$HOME_DIR"
    dscl . -create "/Users/$U" UserShell /usr/bin/false  # no interactive shell
    dscl . -create "/Users/$U" Password "*"              # no password matches "*": no login, no su, no ssh
    dscl . -create "/Users/$U" IsHidden 1                # not in the login window or Users & Groups
    echo "created, uid and gid $ID"
fi
for g in admin staff; do   # a standard user is put in staff by the usual tools; this one must not be
    if dseditgroup -o checkmember -m "$U" "$g" >/dev/null 2>&1; then
        echo "STOP: $U is a member of $g, which would let it read $OWNER_HOME. Nothing else was changed."
        exit 1
    fi
done
id "$U"

step "2. its home $HOME_DIR and its log folder $LOGDIR"
mkdir -p "$HOME_DIR" "$LOGDIR"
chown "$U:$U" "$HOME_DIR" "$LOGDIR"
chmod 700 "$HOME_DIR"
chmod 755 "$LOGDIR"
touch "$LOGDIR/preview.log" "$LOGDIR/isolation-check.log"
chown "$U:$U" "$LOGDIR/preview.log" "$LOGDIR/isolation-check.log"
chmod 644 "$LOGDIR/preview.log" "$LOGDIR/isolation-check.log"
ls -ld "$HOME_DIR" "$LOGDIR"

step "3. the scripts in $LIB, owned by root: readable by $U, not writable"
mkdir -p "$LIB"
chown root:wheel "$LIB"; chmod 755 "$LIB"
install -o root -g wheel -m 755 "$SRC/preview-branch.sh" "$SRC/preview-isolation-check.sh" "$LIB/"
ls -l "$LIB"

step "4. the search model, copied into $U's own Hugging Face cache"
FROM="$OWNER_HOME/.cache/huggingface/hub/$MODEL"
TO="$HOME_DIR/.cache/huggingface/hub/$MODEL"
[ -d "$FROM/snapshots" ] || { echo "no model at $FROM"; exit 1; }
if [ -d "$TO/snapshots" ] && [ "$(cd "$FROM/snapshots" && ls)" = "$(cd "$TO/snapshots" && ls)" ] &&
   [ -n "$(find "$TO/snapshots" -name model.safetensors -size +100M)" ]; then
    echo "already there"
else
    rm -rf "$TO" "$TO.partial"
    mkdir -p "$TO.partial"
    # -L: the owner's cache is links into a shared blob store; the copy holds the files themselves
    cp -RL "$FROM/snapshots" "$FROM/refs" "$TO.partial/"
    [ ! -d "$FROM/.no_exist" ] || cp -RL "$FROM/.no_exist" "$TO.partial/"
    mv "$TO.partial" "$TO"
    echo "copied"
fi
chown -R "$U:$U" "$HOME_DIR/.cache"
chmod -R u+rwX,go-rwx "$HOME_DIR/.cache"
du -sh "$TO"

step "5. $OWNER's own agent removed, so port $PORT is free"
AGENT="$OWNER_HOME/Library/LaunchAgents/$LABEL.plist"
if launchctl print "gui/$OWNER_UID/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "gui/$OWNER_UID/$LABEL" || true
    echo "agent stopped"
else
    echo "agent not loaded"
fi
if [ -f "$AGENT" ]; then rm -f "$AGENT"; echo "removed $AGENT"; else echo "no $AGENT"; fi

listening() { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | grep -F "127.0.0.1:$PORT"; }
stop_daemon() {   # the daemon of an earlier run of this script, if any
    launchctl print "system/$LABEL" >/dev/null 2>&1 || return 0
    launchctl bootout "system/$LABEL" || true
    for _ in $(seq 20); do launchctl print "system/$LABEL" >/dev/null 2>&1 || break; sleep 1; done
}
stop_daemon
for _ in $(seq 15); do listening >/dev/null || break; sleep 1; done
if listening >/dev/null; then   # an app the agent left behind: only ever the preview's own command line
    pkill -U "$OWNER_UID" -f "embed.py serve --demo --port $PORT" || true
    pkill -U "$U" -f "embed.py serve --demo --port $PORT" || true
    sleep 2
fi
if listening; then echo "STOP: something still listens on 127.0.0.1:$PORT (above)"; exit 1; fi
echo "127.0.0.1:$PORT is free"

step "6. the LaunchDaemon $DAEMON, running as $U"
install -o root -g wheel -m 644 "$SRC/$LABEL.plist" "$DAEMON"
plutil -lint "$DAEMON"
launchctl bootstrap system "$DAEMON"
sleep 3
launchctl print "system/$LABEL" | grep -E '^[[:space:]]*(state|username|pid|program) ' || true
echo "log: $LOGDIR/preview.log   (the first start downloads the app's Python packages into"
echo "     $HOME_DIR/.cache/uv, about 1 GB: allow several minutes before port $PORT answers)"

step "7. what $U can reach: a one-off job, run by launchd as $U"
CHECK=com.jflam.photo-preview-check
CHECK_PLIST="$LIB/$CHECK.plist"
cat > "$CHECK_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$CHECK</string>
  <key>UserName</key><string>$U</string>
  <key>GroupName</key><string>$U</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$LIB/preview-isolation-check.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>$HOME_DIR</string>
    <key>USER</key><string>$U</string>
    <key>OWNER_HOME</key><string>$OWNER_HOME</string>
  </dict>
  <key>WorkingDirectory</key><string>$HOME_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOGDIR/isolation-check.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/isolation-check.log</string>
</dict>
</plist>
EOF
chown root:wheel "$CHECK_PLIST"; chmod 644 "$CHECK_PLIST"
launchctl bootout "system/$CHECK" 2>/dev/null || true
: > "$LOGDIR/isolation-check.log"
launchctl bootstrap system "$CHECK_PLIST"
for _ in $(seq 180); do grep -q '^RESULT' "$LOGDIR/isolation-check.log" && break; sleep 1; done
launchctl bootout "system/$CHECK" 2>/dev/null || true   # one-off: it is not left loaded
cat "$LOGDIR/isolation-check.log"
grep -q '^RESULT' "$LOGDIR/isolation-check.log" || echo "(the check did not finish in 3 minutes)"

step "done"
echo "created: user and group $U ($ID), $HOME_DIR, $LIB, $LOGDIR, $DAEMON"
echo "removed: $AGENT"
echo "left alone: $OWNER_HOME/.config/photo-estate/preview-branch.sh, the old clone in"
echo "  $OWNER_HOME/Library/Caches/photo-estate-preview and the old log (none is used any more)"
echo "to undo: sudo launchctl bootout system/$LABEL; sudo rm -rf $DAEMON $LIB $LOGDIR $HOME_DIR;"
echo "  sudo dscl . -delete /Users/$U; sudo dscl . -delete /Groups/$U"
