#!/usr/bin/env bash
# What CI checks (.github/workflows/ci.yml), on this machine, before a pull request is opened, so that it
# arrives already known to pass and nobody waits on GitHub to find out: the lockfile, ruff, pyright, the
# fast tests (in parallel), the script modules' types and unit tests, plus book build and iOS Swift tests
# (on macOS) when their files changed, and the TLA+ models when specs/ changed.
#
#   bash photo-estate/tools/check.sh            # as CI would for this branch
#   bash photo-estate/tools/check.sh --models   # the TLA+ models too, whatever changed
#
# The models (tests/test_specs.py, TLC) are checked when CI would check them: when this branch, with what
# is not yet committed, changes specs/*.tla, specs/*.cfg, or what runs them. They need Java and
# tla2tools.jar (tests/test_specs.py says where); without them the run fails rather than pass unchecked.
#
# Independent checks run in parallel; the first failure prints the end of its output and ends with
# CHECKS FAILED. About 15 seconds on an M-series Mac without models, a minute and a quarter with all models.
set -uo pipefail
cd "$(dirname "$0")/.."
export HF_HUB_OFFLINE=1 UV_PYTHON="${UV_PYTHON:-3.12}"

models=false
book=false
ios=false
[[ "${1:-}" == "--models" ]] && models=true
if base=$(git merge-base origin/main HEAD 2>/dev/null); then
    changed=$( { git diff --name-only "$base"; git ls-files --others --exclude-standard --full-name; } )
    if ! $models; then
        grep -qE '^(photo-estate/specs/[^/]+\.(tla|cfg)$|photo-estate/tests/(test_specs|conftest)\.py$|\.github/workflows/ci\.yml$)' <<<"$changed" && models=true
    fi
    grep -qE '^(docs/book/|docs/screenshots/|photo-estate/tools/build_book\.py$|photo-estate/tools/book_site/)' <<<"$changed" && book=true
    grep -qE '^ios/(Shared/|PhotoEstate/(Catalog|Gate)/|Tests/)' <<<"$changed" && ios=true
else
    models=true            # no origin/main to compare with: check them
fi

began=$SECONDS
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# Fast sequential sanity check before fanning out.
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
[[ -d app/node_modules ]] || step "npm ci" bash -c 'cd app && npm ci'

# Fan out independent checks in parallel, collecting exit code, duration and output per check.
declare -a names=() pids=()
idx=0
launch() {                     # launch <name> <command...>
    local id=$idx name=$1
    shift
    names+=("$name")
    (
        s=$SECONDS
        "$@" >"$tmp/$id.out" 2>&1
        rc=$?
        echo $((SECONDS - s)) >"$tmp/$id.sec"
        exit $rc
    ) &
    pids+=($!)
    idx=$((idx + 1))
}

launch "ruff" uv run --locked ruff check .
launch "pyright" uv run --locked pyright
launch "fast tests (parallel)" uv run --locked pytest -m "not slow" -q --ignore=tests/test_specs.py --deselect=tests/test_types.py::test_pyright_is_clean -n auto
launch "script modules: types & tests" bash -c 'cd app && npm run typecheck && npm test'
if $book; then
    launch "book build" uv run tools/build_book.py "$tmp/site"
fi
if $ios && [[ "$(uname)" == "Darwin" ]]; then
    launch "iOS unit tests" bash -c '../ios/Tests/run.sh && ../ios/Tests/run-replica.sh'
fi

failed_id=""
for i in "${!pids[@]}"; do
    if wait "${pids[$i]}"; then
        sec=$(cat "$tmp/$i.sec" 2>/dev/null || echo 0)
        printf '  ok   %-34s %3ds\n' "${names[$i]}" "$sec"
    else
        sec=$(cat "$tmp/$i.sec" 2>/dev/null || echo 0)
        printf '  FAIL %-34s %3ds\n' "${names[$i]}" "$sec"
        [[ -z "$failed_id" ]] && failed_id=$i
    fi
done

if [[ -n "$failed_id" ]]; then
    printf '\n--- %s ---\n' "${names[$failed_id]}"
    tail -n 60 "$tmp/$failed_id.out"
    printf '\nCHECKS FAILED: %s (%ds)\n' "${names[$failed_id]}" $((SECONDS - began))
    exit 1
fi

if $models; then
    step "TLA+ models (TLC)" env REQUIRE_TLC=1 uv run --locked pytest tests/test_specs.py -q
else
    printf '  skip %-34s\n' "TLA+ models (specs unchanged)"
fi

printf 'CHECKS PASSED (%ds)\n' $((SECONDS - began))
