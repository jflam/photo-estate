#!/usr/bin/env bash
# Shows the web app of this checkout, on the demo library, to a person: for a look at work in a cloud
# session (Claude Code on the web) before it is merged. A cloud session takes no connections from
# outside, so the app is carried out through tailcat (github.com/tailscale/tailcat: Tailscale's
# encrypted pipe, without a tailnet or an account) to the Mac mini, which forwards a port of its own
# to it (deploy/preview-relay.sh) and shows that port on the tailnet. See deploy/MAC-MINI.md.
#
#   bash photo-estate/tools/cloud-preview.sh             start (or restart) the app and the pipe
#   bash photo-estate/tools/cloud-preview.sh --install   only install tailcat (cloud-setup.sh does this)
#   bash photo-estate/tools/cloud-preview.sh --stop      stop both
#
# The environment gives it, as secrets:
#   PHOTO_ESTATE_PREVIEW_KEY   the saved server key (photo-estate-preview.private.json), as base64 on one
#                              line (`base64 < the-file`), which an environment's KEY=value settings
#                              take as it is; the file's own JSON is taken too. The mini knows this
#                              key's address, so it must be this key.
#   PHOTO_ESTATE_PREVIEW_URL   where a person opens the preview (the mini's tailnet address); only
#                              printed, and not kept here because this repository is public.
# Only the demo library is ever served, and only the mini can connect (--allow).
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # photo-estate/photo-estate
PORT=8767
TAILCAT_VERSION=0.7.0
ALLOW="${PHOTO_ESTATE_PREVIEW_ALLOW:-nodekey:a8cc02f1c05e511177c6207dbd499436f2d39e8c0f00c6965845fd8986ccd816}"   # the mini's client key (public)
STATE="$HOME/.config/photo-estate"
KEY_FILE="$STATE/preview.private.json"
export PATH="$HOME/.local/bin:$PATH"
hide() { sed -E 's/tc[A-Za-z0-9_+\/=-]{20,}/tc<address>/g'; }   # the address is a secret: never in a log a person reads

install_tailcat() {
    command -v tailcat >/dev/null && return 0
    case "$(uname -s)-$(uname -m)" in
        Linux-x86_64) arch=amd64 ;;
        Linux-aarch64 | Linux-arm64) arch=arm64 ;;
        *) echo "tailcat: install it yourself here (brew install tailcat)"; return 1 ;;
    esac
    local name="tailcat_${TAILCAT_VERSION}_linux_${arch}.tar.gz" url="https://github.com/tailscale/tailcat/releases/download/v${TAILCAT_VERSION}"
    local tmp; tmp="$(mktemp -d)"
    if curl -fsSL -o "$tmp/$name" "$url/$name" && curl -fsSL -o "$tmp/checksums.txt" "$url/checksums.txt" \
        && (cd "$tmp" && grep " $name\$" checksums.txt | sha256sum -c --quiet -) && tar -xzf "$tmp/$name" -C "$tmp"; then
        mkdir -p "$HOME/.local/bin" && install -m 755 "$(find "$tmp" -type f -name tailcat | head -1)" "$HOME/.local/bin/tailcat"
    fi
    command -v tailcat >/dev/null || { echo "tailcat: could not be downloaded from github.com/tailscale/tailcat (allow github.com and release-assets.githubusercontent.com)"; return 1; }
}

stop() {
    pkill -f "tailcat serve --key=$KEY_FILE" 2>/dev/null
    pkill -f "embed.py serve --demo --port $PORT" 2>/dev/null
    return 0
}

case "${1:-}" in
    --install) install_tailcat; exit $? ;;
    --stop) stop; echo "preview stopped"; exit 0 ;;
esac

[ -n "${PHOTO_ESTATE_PREVIEW_KEY:-}" ] || { echo "PREVIEW NOT STARTED: this environment has no PHOTO_ESTATE_PREVIEW_KEY (see deploy/MAC-MINI.md, \"A preview of a cloud session\")"; exit 1; }
install_tailcat || { echo "PREVIEW NOT STARTED: no tailcat"; exit 1; }
mkdir -p "$STATE" && (umask 077; case "$PHOTO_ESTATE_PREVIEW_KEY" in
    "{"*) printf '%s' "$PHOTO_ESTATE_PREVIEW_KEY" ;;
    *) printf '%s' "$PHOTO_ESTATE_PREVIEW_KEY" | base64 -d ;;
esac > "$KEY_FILE")
grep -q '"' "$KEY_FILE" || { echo "PREVIEW NOT STARTED: PHOTO_ESTATE_PREVIEW_KEY is not the key file's contents, or its base64"; exit 1; }

# the app: started again each time, so it is this checkout as it is now (a page edit alone needs no restart)
stop; sleep 1
# (both are cut off from this script's output, so whatever ran the script is not left waiting for them)
(cd "$HERE" && HF_HUB_OFFLINE=1 exec nohup uv run --quiet embed.py serve --demo --port "$PORT") > "$STATE/preview-app.log" 2>&1 < /dev/null &
for _ in $(seq 1 120); do curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/info" && break; sleep 1; done
curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/info" || { echo "PREVIEW NOT STARTED: the app did not start:"; tail -n 15 "$STATE/preview-app.log"; exit 1; }

# the pipe: this app's port and nothing else, to the mini and no one else
nohup tailcat serve --key="$KEY_FILE" --allow="$ALLOW" "$PORT" > "$STATE/preview-tailcat.log" 2>&1 < /dev/null &
for _ in $(seq 1 30); do grep -q "Server listening" "$STATE/preview-tailcat.log" && break; sleep 1; done
if ! grep -q "Server listening" "$STATE/preview-tailcat.log"; then
    echo "PREVIEW NOT STARTED: tailcat could not reach its relay (allow tailcat.dev and tc302a.ipn.dev):"
    hide < "$STATE/preview-tailcat.log" | tail -n 10
    exit 1
fi
echo "PREVIEW STARTED: the demo app of this checkout is being served to the mini."
echo "Open: ${PHOTO_ESTATE_PREVIEW_URL:-port 8768 of the Mac mini, on the tailnet (set PHOTO_ESTATE_PREVIEW_URL to have the address here)}"
echo "The mini looks for it every few seconds: allow a minute. It stops when this session's machine does."
