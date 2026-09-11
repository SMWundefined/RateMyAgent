#!/usr/bin/env bash
#
# Wait for an HTTP endpoint to become ready, and fail loudly if it never does.
#
# **A script rather than a loop inlined in the workflow**, because the previous
# version of this could not be tested: the body lived in a `run:` block, and the
# only way to exercise it was to paste it into a terminal -- which runs it under
# a different shell than CI does. That is precisely how the bug this replaces
# got shipped. Here the file CI executes and the file the suite executes are the
# same file.
#
#   wait-for-endpoint.sh stream  http://localhost:3005/sse   [attempts] [interval]
#   wait-for-endpoint.sh respond http://localhost:3001/mcp   [attempts] [interval]
#
# Two modes, because "ready" is a different observation for the two endpoint
# shapes and conflating them is the original defect:
#
#   stream   The endpoint holds the connection open (Server-Sent Events). A bare
#            GET can never return, so readiness is curl exiting **28** -- timed
#            out with bytes received -- under `--max-time`. That proves the
#            endpoint is serving, not merely that the port is open. `0` counts
#            too: the stream closed early, which still means it answered.
#
#   respond  The endpoint answers and closes. Readiness is an HTTP status in
#            2xx or 4xx -- a 400 to a bare GET means it is up and objecting,
#            which is enough.
#
# Every attempt logs its exit code and elapsed time. "gave up after 30" says
# nothing; "eighteen connection-refused then a 28 at 6s" says what happened, and
# a failure that cannot describe itself makes the next person start over.

set -uo pipefail

MODE="${1:?usage: wait-for-endpoint.sh <stream|respond> <url> [attempts]}"
URL="${2:?usage: wait-for-endpoint.sh <stream|respond> <url> [attempts]}"
ATTEMPTS="${3:-30}"
# Seconds between polls. A parameter only so the suite can exercise the retry
# path many times without spending a second per attempt; CI takes the default.
INTERVAL="${4:-1}"

started=$(date +%s)

for attempt in $(seq 1 "$ATTEMPTS"); do
    elapsed=$(( $(date +%s) - started ))

    case "$MODE" in
        stream)
            # `code=0; cmd || code=$?` and never a bare `cmd`. Under `bash -e`,
            # which is what GitHub runs `run:` steps with, a bare command that
            # exits non-zero kills the script before the next line executes --
            # so the retry loop never retried, and the first connection-refused
            # ended the step. A tested context is what makes the failure a value
            # this script can act on rather than a signal that terminates it.
            code=0
            curl -s --max-time 2 -o /dev/null "$URL" || code=$?
            echo "  attempt ${attempt}/${ATTEMPTS} t=${elapsed}s curl_exit=${code}"
            case "$code" in
                28|0) echo "ready: streaming at ${URL} after ${elapsed}s"; exit 0 ;;
            esac
            ;;
        respond)
            status=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$URL" || true)
            echo "  attempt ${attempt}/${ATTEMPTS} t=${elapsed}s http_status=${status}"
            case "$status" in
                2*|4*) echo "ready: ${URL} answered ${status} after ${elapsed}s"; exit 0 ;;
            esac
            ;;
        *)
            echo "::error::unknown mode '${MODE}' (expected 'stream' or 'respond')"
            exit 2
            ;;
    esac

    sleep "$INTERVAL"
done

# It has to be able to fail. The loop this replaces fell through after its
# attempts with no assertion, so a server that never started would have produced
# a green readiness step and a baffling failure further down.
echo "::error::${URL} never became ready after ${ATTEMPTS} attempts ($(( $(date +%s) - started ))s)"
exit 1
