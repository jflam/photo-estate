#!/bin/zsh
# The phone's copy of the catalog, on the Mac (no device, no Simulator): PhotoEstate/Catalog/Catalog.swift on
# PhotoEstate/Catalog/SQLite.swift, compiled with Tests/Replica/main.swift and run: a sync's pages and a kept favourite,
# in the orders specs/PhoneReplica.tla found (photo-estate/tests/test_phone_replica_states.py is the server's half).
#   ios/Tests/run-replica.sh
set -euo pipefail
cd "$(dirname "$0")/.."
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
xcrun swiftc -swift-version 5 -suppress-warnings -o "$out/replica-tests" \
    PhotoEstate/Catalog/SQLite.swift PhotoEstate/Catalog/Catalog.swift Tests/Replica/main.swift
"$out/replica-tests"
