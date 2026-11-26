#!/bin/zsh
# The phone's unit tests, on the Mac (no device, no Simulator): Shared/PhoneGate.swift, the request building in
# Shared/UploadRequest.swift and the questions and stories in PhotoEstate/Gate/StoryModel.swift, compiled with
# Tests/main.swift and run.
#   ios/Tests/run.sh
set -euo pipefail
cd "$(dirname "$0")/.."
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
xcrun swiftc -swift-version 5 -suppress-warnings -o "$out/gate-tests" \
    Shared/PhoneGate.swift Shared/UploadRequest.swift Shared/Server.swift Shared/ExtensionStatus.swift \
    PhotoEstate/Gate/StoryModel.swift Tests/main.swift
"$out/gate-tests"
