#!/usr/bin/env bash
# Ovid2 quality gates — the single source of truth for "is this tree green".
#
# GitHub Actions (.github/workflows/ci.yml) runs each gate as its own job by
# calling this script, and scripts/public/publish.py runs it on the
# sanitised public tree before pushing. Keeping the commands here means CI
# and the publish gate can never drift apart again.
#
# Usage:
#   scripts/ci_checks.sh [GATE ...]          # run in the current Python env
#   scripts/ci_checks.sh --docker [GATE ...] # run inside a python:3.12 image
#                                            # with the CI dependency set
# Gates: lint, typecheck, contract, unit   (default: all four, in order)
#
# The tree is bind-mounted read-write (like a CI checkout): some tests
# create gitignored scratch dirs such as ``output/``.
#
# --docker builds (once, cached by a hash of pyproject.toml) an image that
# mirrors the CI install, so local results match CI even when the host
# Python or its packages differ. Override the base image with
# OVID2_CI_PYTHON_IMAGE (default python:3.12-slim).
set -euo pipefail

cd "$(dirname "$0")/.."

ALL_GATES=(lint typecheck contract unit)
UNIT_MARKERS="not contract and not integration and not real_connection and not real_cluster and not perf and not endurance"

if [ "${1:-}" = "--docker" ]; then
    shift
    base="${OVID2_CI_PYTHON_IMAGE:-python:3.12-slim}"
    tag="ovid2-ci:$(sha256sum pyproject.toml | cut -c1-12)"
    if ! docker image inspect "$tag" >/dev/null 2>&1; then
        ctx="$(mktemp -d)"
        trap 'rm -rf "$ctx"' EXIT
        python3 - "$ctx/reqs.txt" <<'PY'
import re, sys
s = open("pyproject.toml", encoding="utf-8").read()
def block(name: str) -> list[str]:
    m = re.search(r"^" + name + r"\s*=\s*\[(.*?)^\]", s, re.S | re.M)
    return re.findall(r'^\s*"([^"]+)"', m.group(1), re.M) if m else []
reqs = block("dependencies")
for extra in ("dev", "spark", "web", "mcp", "azure"):
    reqs += block(extra)
open(sys.argv[1], "w", encoding="utf-8").write("\n".join(reqs) + "\n")
PY
        cat >"$ctx/Dockerfile" <<EOF
FROM $base
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends openjdk-21-jre-headless >/dev/null && rm -rf /var/lib/apt/lists/*
COPY reqs.txt /reqs.txt
RUN pip install -q --upgrade pip && pip install -q -r /reqs.txt
EOF
        echo "[ci_checks] building $tag (first run only) ..."
        docker build -q -t "$tag" "$ctx" >/dev/null
    fi
    # Hadoop's UGI login (Spark startup) needs a passwd entry for the uid.
    pw="$(mktemp)"
    printf 'ci:x:%s:%s:ci:/tmp:/bin/sh\n' "$(id -u)" "$(id -g)" >"$pw"
    exec docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -e USER=ci \
        -v "$pw:/etc/passwd:ro" \
        -e PYTHONPATH=/w/src:/w/pipelines -e PYTHONDONTWRITEBYTECODE=1 \
        -e MYPY_CACHE_DIR=/tmp/mypy_cache -e RUFF_CACHE_DIR=/tmp/ruff_cache \
        -v "$PWD:/w" -w /w "$tag" bash scripts/ci_checks.sh "$@"
fi

gates=("$@")
[ ${#gates[@]} -eq 0 ] && gates=("${ALL_GATES[@]}")

failed=()
for gate in "${gates[@]}"; do
    echo "::group::[ci_checks] $gate"
    rc=0
    case "$gate" in
        lint)
            ruff check --no-cache . && ruff format --check . || rc=$?
            ;;
        typecheck)
            mypy --config-file=pyproject.toml src/ovid2 || rc=$?
            ;;
        contract)
            pytest -q -p no:cacheprovider -m contract || rc=$?
            ;;
        unit)
            pytest -q -p no:cacheprovider -m "$UNIT_MARKERS" || rc=$?
            ;;
        *)
            echo "unknown gate: $gate (expected one of: ${ALL_GATES[*]})" >&2
            rc=2
            ;;
    esac
    echo "::endgroup::"
    if [ "$rc" -ne 0 ]; then
        echo "[ci_checks] $gate FAILED (exit $rc)"
        failed+=("$gate")
    else
        echo "[ci_checks] $gate passed"
    fi
done

if [ ${#failed[@]} -gt 0 ]; then
    echo "[ci_checks] FAILED: ${failed[*]}"
    exit 1
fi
echo "[ci_checks] all gates passed: ${gates[*]}"
