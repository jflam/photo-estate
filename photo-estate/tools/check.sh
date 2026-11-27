#!/usr/bin/env bash
# What CI checks (.github/workflows/ci.yml), on this machine, before a pull request is opened, so that it
# arrives already known to pass and nobody waits on GitHub to find out: the lockfile, ruff, pyright, the
# fast tests, and the script modules' types and unit tests.
#
#   bash photo-estate/tools/check.sh            # as CI would for this branch
#   bash photo-estate/tools/check.sh --models   # the TLA+ models too, whatever changed
#
# The models (tests/test_specs.py, TLC) are checked when CI would check them: when this branch, with what
# is not yet committed, changes specs/ or what runs them. They need Java and tla2tools.jar
# (tests/test_specs.py says where); without them the run fails rather than pass unchecked.
#
# It stops at the first step that fails, shows the end of that step's output, and ends with one line:
# CHECKS PASSED or CHECKS FAILED. About a minute and a half on an M-series Mac, three with the models.
set -uo pipefail
cd "$(dirname "$0")/.."
export HF_HUB_OFFLINE=1 UV_PYTHON="${UV_PYTHON:-3.12}"

models=false
[[ "${1:-}" == "--models" ]] && models=true
if ! $models; then
    if base=$(git merge-base origin/main HEAD 2>/dev/null); then
        changed=$( { git diff --name-only "$base"; git ls-files --others --exclude-standard --full-name; } )
        grep -qE '^(photo-estate/specs/|photo-estate/tests/(test_specs|conftest)\.py$|\.github/workflows/ci\.yml$)' <<<"$changed" && models=true
    else
        models=true            # no origin/main to compare with: check them
    fi
fi

began=$SECONDS
step() {                       # step <name> <command...>
    local name=$1 start=$SECONDS out
    shift
    if out=$("$@" 2>&1); then
        printf '  ok   %-34s %3ds\n' "$name" $((SECONDS - start))
    else
        printf '  FAIL %-34s %3ds\n\n' "$name" $((SECONDS - start))
        tail -n 60 <<<"$out"
        printf '\nCHECKS FAILED: %s (%ds)\n' "$name" $((SECONDS - began))
        exit 1
    fi
}

step "lockfile" uv lock --check
step "ruff" uv run --locked ruff check .
step "pyright" uv run --locked pyright
if $models; then
    step "fast tests and the models" env REQUIRE_TLC=1 uv run --locked pytest -m "not slow" -q
else
    step "fast tests (models not needed)" uv run --locked pytest -m "not slow" -q --ignore=tests/test_specs.py
fi
cd app
[[ -d node_modules ]] || step "npm ci" npm ci
step "script modules: types" npm run typecheck
step "script modules: unit tests" npm test
printf 'CHECKS PASSED (%ds)\n' $((SECONDS - began))
