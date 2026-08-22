#!/usr/bin/env bash
# Every check CI runs, in one command, failing on the first one that fails.
#
# This exists because of a specific mistake. Verifying by hand as
# `make -C reference/c san 2>&1 | tail -1` reports the exit status of `tail`,
# not of `make` -- so a build that did not compile printed its last harmless
# line and looked like a pass, and a red CI reached review. `set -euo pipefail`
# is the whole fix, and having one script means nobody has to remember it.
#
#   tools/ci.sh          run everything
#   tools/ci.sh --quick  skip the mutation sweep, which dominates the runtime
#
# Nothing here needs a Bluetooth adapter, including the harness step: its
# loopback transport drives the software peripheral in-process.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

quick=0
[ "${1:-}" = "--quick" ] && quick=1

step() { printf '\n=== %s\n' "$1"; }

step "generated artefacts are up to date"
python3 tools/generate.py --check

step "documentation claims match the artefacts"
python3 tools/check_docs.py

step "the corpus can detect a violation of every rule"
python3 tools/check_corpus.py

step "build"
make -C reference/c

step "conformance — C"
python3 conformance/run.py --impl "reference/c/vtp1_cli"

step "conformance — Python"
python3 conformance/run.py --impl "python3 reference/python/vtp1.py"

step "producers — C"
python3 conformance/produce.py --impl "reference/c/vtp1_producer"

step "producers — Python"
python3 conformance/produce.py --impl "python3 reference/python/vtp1_produce.py"

step "roles can be declared"
python3 conformance/run.py --impl "reference/c/vtp1_cli" --roles gps
python3 conformance/produce.py --impl "reference/c/vtp1_producer" --roles can,monitor

step "software peripheral conforms"
python3 reference/peripheral/selftest.py

step "transport state machine conforms"
python3 reference/peripheral/transport_selftest.py

step "conformance harness conforms, and detects every defect it claims"
PYTHONPATH=harness python3 harness/selftest.py

step "no existing vector changed meaning"
python3 tools/check_baseline.py --check

step "C encoder API contract, sanitised"
make -C reference/c san

if [ "$quick" = "0" ]; then
    step "the corpus has no coverage holes"
    python3 tools/mutate.py
fi

printf '\nAll checks passed.\n'
