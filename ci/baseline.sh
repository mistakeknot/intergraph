#!/usr/bin/env bash
# Manifest-derived, secret-free baseline for intergraph, from the documented test
# command: Python 3 standard library only, `python3 -I -m unittest discover -s tests`,
# with a fresh writable TMPDIR and HOME. Run from a checkout; the fleet copies this
# file outside the source tree. Usage: ci/baseline.sh [--check]
set -euo pipefail
if [[ ! -d tests || ! -d intergraph ]]; then
  cd "$(dirname "${BASH_SOURCE[0]}")/.."
fi
[[ -d tests && -d intergraph ]]
mode=${1:-run}
[[ $# -le 1 && ( $mode == run || $mode == --check ) ]]
echo "RECIPE intergraph baseline v1"
head=$(git rev-parse HEAD)
status=$(git status --porcelain)
echo "HEAD $head"
echo "TREE_DIRTY $(printf '%s' "$status" | grep -c . || true)"
python3 --version
echo 'PLAN python3 -I -B -m unittest discover -s tests; offline, fresh HOME and TMPDIR, no bytecode writes'
echo 'EXCLUDED nothing: the suite has no network, credential or service dependencies'
if [[ $mode == --check ]]; then exit 0; fi
if [[ -n ${CI_SOURCE_SHA:-} ]]; then
  [[ $head == "$CI_SOURCE_SHA" ]]
  [[ -z $status ]]
fi
[[ $(uname -s) == Linux ]]
SCRATCH=$(mktemp -d)
trap 'rm -rf -- "$SCRATCH"' EXIT
mkdir -p "$SCRATCH/home" "$SCRATCH/tmp"
export HOME=$SCRATCH/home TMPDIR=$SCRATCH/tmp
if nice -n 19 timeout 900 python3 -I -B -m unittest discover -s tests -v > "$SCRATCH/unittest.log" 2>&1; then
  tail -n 4 "$SCRATCH/unittest.log"
  echo "CHECK unittest PASS"
else
  rc=$?
  tail -n 60 "$SCRATCH/unittest.log"
  echo "CHECK unittest FAIL ($rc)"
  exit "$rc"
fi
