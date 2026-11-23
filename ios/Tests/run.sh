#!/bin/zsh
# The phone gate's unit tests, on the Mac (no device, no Simulator): Shared/PhoneGate.swift and the request
# building in Shared/UploadRequest.swift, compiled with Tests/main.swift and run.
#   ios/Tests/run.sh
set -euo pipefail
cd "$(dirname "$0")/.."
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
xcrun swiftc -swift-version 5 -suppress-warnings -o "$out/gate-tests" \
    Shared/PhoneGate.swift Shared/UploadRequest.swift Shared/Server.swift Shared/ExtensionStatus.swift Tests/main.swift
"$out/gate-tests"
