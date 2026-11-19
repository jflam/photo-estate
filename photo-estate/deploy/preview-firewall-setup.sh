#!/bin/bash
# Stops the cloud preview's user, photopreview, from opening connections to this machine's own
# services (the real app on 127.0.0.1:8766 among them) and to the private networks the machine is on.
# A separate user keeps it out of files; it does not keep it off the loopback. This adds rules to the
# macOS packet filter (pf) that match that user only, and loads them at every boot.
# Run after preview-user-setup.sh, which makes the user and the check this ends with.
#
#   sudo bash preview-firewall-setup.sh      (needs root; safe to run again; prints each step)
#
# It turns pf on (macOS ships it off, with a main ruleset that holds only Apple's empty anchors) and
# adds two lines to /etc/pf.conf, kept in /etc/pf.conf.before-photo-preview as it was. No rule here
# matches any other user, so the real app, the demo, Tailscale and SSH are not filtered.
set -euo pipefail

U=photopreview
PORT=8768                               # the preview's own port: it may still talk to itself
ANCHOR=com.jflam.photo-preview
RULES=/etc/pf.anchors/$ANCHOR
LIB=/usr/local/photo-preview
LOADER=$LIB/firewall-load.sh
LOGDIR=/var/log/photo-preview
LABEL=com.jflam.photo-preview-firewall
DAEMON=/Library/LaunchDaemons/$LABEL.plist
CHECK=com.jflam.photo-preview-check
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE"; [ -f "$SRC/preview-isolation-check.sh" ] || SRC="$HERE/photo-preview-files"

step() { printf '\n== %s\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run this with sudo"; exit 1; }
id "$U" >/dev/null 2>&1 || { echo "no user $U: run preview-user-setup.sh first"; exit 1; }
[ -f "$LIB/$CHECK.plist" ] || { echo "no $LIB/$CHECK.plist: run preview-user-setup.sh first"; exit 1; }
[ -f "$SRC/preview-isolation-check.sh" ] || { echo "missing preview-isolation-check.sh beside this script"; exit 1; }

step "1. the rules, in $RULES"
NEW="$(mktemp)"
cat > "$NEW" <<EOF
# The cloud preview's user may not open connections to this machine or to private networks.
# Installed by preview-firewall-setup.sh (photo-estate/deploy). Every rule matches user $U only.
# "flags S/SA" matches the first packet of a TCP connection, so connections that $U starts are
# refused, and the answers its own server on port $PORT sends to Tailscale's proxy are not.
table <photopreview_private> const { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16, fc00::/7, fe80::/10 }

# 1. anything on the loopback, except its own app
block return out quick on lo0 proto tcp from any to any port != $PORT flags S/SA user $U
block return out quick on lo0 proto udp from any to any user $U
# 2. this machine's other addresses (LAN, tailnet), whichever interface pf sees them on
block return out quick proto tcp from any to self port != $PORT flags S/SA user $U
block return out quick proto udp from any to self user $U
# 3. the home network and the tailnet. Not needed for the loopback: delete these two to allow them.
block return out quick proto tcp from any to <photopreview_private> flags S/SA user $U
block return out quick proto udp from any to <photopreview_private> user $U
EOF
pfctl -nf "$NEW" 2>/dev/null || { echo "STOP: pf does not accept the rules; nothing was changed"; pfctl -nf "$NEW" || true; rm -f "$NEW"; exit 1; }
install -o root -g wheel -m 644 "$NEW" "$RULES"; rm -f "$NEW"
cat "$RULES"

step "2. the loader $LOADER: puts the anchor in /etc/pf.conf if it is not there, loads it, turns pf on"
cat > "$LOADER" <<EOF
#!/bin/bash
# Run by $LABEL at boot, and by preview-firewall-setup.sh. A macOS upgrade can put back the
# stock /etc/pf.conf, so the anchor's two lines are added again whenever they are missing.
set -u
CONF=/etc/pf.conf
if ! grep -q '^anchor "$ANCHOR"' "\$CONF"; then
    [ -f "\$CONF.before-photo-preview" ] || cp -p "\$CONF" "\$CONF.before-photo-preview"
    cp -p "\$CONF" "\$CONF.new"
    printf '\n# the cloud preview: its user may not reach this machine (photo-estate/deploy/preview-firewall-setup.sh)\nanchor "$ANCHOR"\nload anchor "$ANCHOR" from "$RULES"\n' >> "\$CONF.new"
    if pfctl -nf "\$CONF.new" 2>/dev/null; then mv "\$CONF.new" "\$CONF"; echo "\$(date '+%F %T') anchor added to \$CONF"
    else rm -f "\$CONF.new"; echo "\$(date '+%F %T') FAILED: pf refused the changed \$CONF, which is left as it was"; exit 1; fi
fi
pfctl -f "\$CONF" 2>/dev/null || { echo "\$(date '+%F %T') FAILED: could not load \$CONF"; exit 1; }
pfctl -s info 2>/dev/null | grep -q '^Status: Enabled' || pfctl -E >/dev/null 2>&1
if pfctl -s info 2>/dev/null | grep -q '^Status: Enabled' && pfctl -a "$ANCHOR" -s rules 2>/dev/null | grep -q block; then
    echo "\$(date '+%F %T') pf is on and the rules for $U are loaded"
else
    echo "\$(date '+%F %T') FAILED: pf is off or the rules are missing"; exit 1
fi
EOF
chown root:wheel "$LOADER"; chmod 755 "$LOADER"
echo "written"

step "3. the LaunchDaemon $DAEMON, which runs the loader now and at every boot"
cat > "$DAEMON" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$LOADER</string></array>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOGDIR/firewall.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/firewall.log</string>
</dict>
</plist>
EOF
chown root:wheel "$DAEMON"; chmod 644 "$DAEMON"
launchctl bootout "system/$LABEL" 2>/dev/null || true
bash "$LOADER" || { echo "STOP: the loader failed (above)"; exit 1; }
launchctl bootstrap system "$DAEMON"
echo "-- /etc/pf.conf now ends:"; tail -n 3 /etc/pf.conf
echo "-- pf:"; pfctl -s info 2>/dev/null | head -n 1
echo "-- rules loaded for $U:"; pfctl -a "$ANCHOR" -s rules 2>/dev/null

step "4. what $U can reach now: the one-off job again, run by launchd as $U"
install -o root -g wheel -m 755 "$SRC/preview-isolation-check.sh" "$LIB/"
launchctl bootout "system/$CHECK" 2>/dev/null || true
: > "$LOGDIR/isolation-check.log"
launchctl bootstrap system "$LIB/$CHECK.plist"
for _ in $(seq 240); do grep -q '^RESULT' "$LOGDIR/isolation-check.log" && break; sleep 1; done
launchctl bootout "system/$CHECK" 2>/dev/null || true
cat "$LOGDIR/isolation-check.log"
grep -q '^RESULT' "$LOGDIR/isolation-check.log" || echo "(the check did not finish in 4 minutes)"

step "done"
echo "created: $RULES, $LOADER, $DAEMON, $LOGDIR/firewall.log; two lines at the end of /etc/pf.conf"
echo "to undo: sudo launchctl bootout system/$LABEL; sudo rm -f $DAEMON $LOADER $RULES;"
echo "  sudo cp -p /etc/pf.conf.before-photo-preview /etc/pf.conf; sudo pfctl -f /etc/pf.conf; sudo pfctl -d"
