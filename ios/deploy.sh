#!/bin/zsh
# Build the app and put it on the iPhone, as the next build number. The number is in `build-number`
# and shows at the foot of the app's settings ("Build 7"), so the phone can be checked against it.
#   ./deploy.sh              the first iPhone that is connected or on the network
#   ./deploy.sh <name|udid>  that one
set -euo pipefail
cd "$(dirname "$0")"

device=${1:-$(xcrun devicectl list devices | grep physical | grep iPhone | grep -oE '[0-9A-F]{8}-[0-9A-F]{16}' | head -1)}
[[ -n "$device" ]] || { echo "no iPhone found (xcrun devicectl list devices)"; exit 1; }
n=$(( $(cat build-number 2>/dev/null || echo 0) + 1 ))

xcodegen --quiet
xcodebuild -project PhotoEstate.xcodeproj -scheme PhotoEstate -destination generic/platform=iOS \
    -derivedDataPath build-device -allowProvisioningUpdates CURRENT_PROJECT_VERSION=$n build | grep -E "error:|BUILD" || true
app=build-device/Build/Products/Debug-iphoneos/PhotoEstate.app
[[ "$(/usr/libexec/PlistBuddy -c 'Print CFBundleVersion' $app/Info.plist)" == "$n" ]] || { echo "build $n was not made"; exit 1; }
# The first try often finds the phone asleep: it wakes the connection, and the next one goes through.
for try in 1 2 3; do
    xcrun devicectl device install app --device "$device" $app && break
    [[ $try == 3 ]] && { echo "could not reach the iPhone: unlock it and run this again"; exit 1; }
    sleep 8
done
echo $n > build-number        # only once it is on the phone
echo "Build $n is on the iPhone."
