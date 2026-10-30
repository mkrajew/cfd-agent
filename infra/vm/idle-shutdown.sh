#!/usr/bin/env bash
# Deallocate this VM after 30 consecutive idle minutes.
#
# Run by cfdagent-idle-shutdown.timer every 5 minutes. The VM is busy when any of these holds:
#   - a login session exists (who),
#   - an established SSH connection exists (covers Remote-SSH sessions that have no tty),
#   - a cfdagent process is running (also inside a container),
#   - a container from the cfd-agent image is running.
# The idle start time lives in $STATE_DIR/idle-since. Deallocation uses the ARM REST API with the
# token of the VM's managed identity (IMDS); the identity may deallocate only this VM. A failed
# attempt is retried 3 times; if all fail the script exits non-zero and keeps its state, so the
# next timer run tries again.
#
# Usage: cfdagent-idle-shutdown [--dry-run | --force | --daily | --check] [--dry-run]
#   --dry-run  take the decision and print it, but do not call Azure (also combines with
#              --force and --daily).
#   --force    deallocate now, whatever is running.
#   --daily    used by the daily-shutdown timer, which fires every 10 minutes in a window after the
#              shutdown time. While $STATE_DIR/active-run exists and its modification time is
#              younger than 4 hours (a `cfdagent run` is under way) it logs "deferred: active run"
#              and exits 0 without deallocating, so the next timer run asks again. Otherwise it
#              behaves like --force. A marker that is older, dated in the future or not a regular
#              file is ignored. The script never creates or removes the marker.
#   --check    get an IMDS token and read this VM through ARM; proves identity, role assignment
#              and network without deallocating anything.
# Test hooks (environment): CFDAGENT_STATE_DIR, CFDAGENT_IDLE_MINUTES, CFDAGENT_IMAGE,
#   CFDAGENT_NOW (epoch seconds), CFDAGENT_UPTIME_SECONDS, CFDAGENT_RETRY_SLEEP (seconds).
set -euo pipefail

STATE_DIR="${CFDAGENT_STATE_DIR:-/var/lib/cfdagent}"
IDLE_MINUTES="${CFDAGENT_IDLE_MINUTES:-30}"
IMAGE_REPO="${CFDAGENT_IMAGE:-ghcr.io/mkrajew/cfd-agent}"
RETRY_SLEEP="${CFDAGENT_RETRY_SLEEP:-10}"
IDLE_FILE="${STATE_DIR}/idle-since"
ACTIVE_FILE="${STATE_DIR}/active-run"
ACTIVE_MAX_SECONDS=$((4 * 3600))
IMDS="http://169.254.169.254/metadata"
ARM="https://management.azure.com"
ARM_API_VERSION="2024-07-01"
ATTEMPTS=3

MODE=idle
DRY=0
for arg in "$@"; do
    case "${arg}" in
        --dry-run) DRY=1 ;;
        --force) MODE=force ;;
        --daily) MODE=daily ;;
        --check) MODE=check ;;
        *)
            echo "usage: $0 [--dry-run | --force | --daily | --check] [--dry-run]" >&2
            exit 2
            ;;
    esac
done

log() { echo "$*"; }
err() { echo "$*" >&2; }

# Print why the VM is busy and return 0, or return 1 when it is idle.
busy_reason() {
    local out
    out="$(who 2>/dev/null || true)"
    if [ -n "${out}" ]; then
        echo "login session"
        return 0
    fi
    out="$(ss -Htn state established '( sport = :22 )' 2>/dev/null || true)"
    if [ -n "${out}" ]; then
        echo "ssh connection"
        return 0
    fi
    if pgrep -x cfdagent >/dev/null 2>&1; then
        echo "cfdagent process"
        return 0
    fi
    if pgrep -f -- '-m cfdagent( |$)' >/dev/null 2>&1; then
        echo "cfdagent process"
        return 0
    fi
    if command -v docker >/dev/null 2>&1; then
        out="$(docker ps --format '{{.Image}}' 2>/dev/null || true)"
        if grep -Eq "^${IMAGE_REPO}(:|@|\$)" <<<"${out}"; then
            echo "cfd-agent container"
            return 0
        fi
    fi
    return 1
}

json_field() {
    python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

# http <curl arguments ..., url last>: sets HTTP_CODE (000 when curl itself failed) and HTTP_BODY.
HTTP_CODE=000
HTTP_BODY=
http() {
    local tmp
    tmp="$(mktemp)"
    HTTP_CODE="$(curl -sS --fail-with-body --max-time 30 -o "${tmp}" -w '%{http_code}' "$@" 2>/dev/null)" || true
    [ -n "${HTTP_CODE}" ] || HTTP_CODE=000
    HTTP_BODY="$(cat "${tmp}" 2>/dev/null || true)"
    rm -f "${tmp}"
}

ok_code() { case "$1" in 2??) return 0 ;; *) return 1 ;; esac; }

# Sets VM_URL and TOKEN from IMDS, or returns 1 after logging why.
SUB=
RG=
VM=
TOKEN=
VM_URL=
get_identity() {
    http -H 'Metadata:true' "${IMDS}/instance/compute?api-version=2021-02-01&format=json"
    if ! ok_code "${HTTP_CODE}"; then
        err "IMDS metadata failed (HTTP ${HTTP_CODE})"
        return 1
    fi
    SUB="$(json_field subscriptionId <<<"${HTTP_BODY}" 2>/dev/null)" || {
        err "IMDS metadata unreadable"
        return 1
    }
    RG="$(json_field resourceGroupName <<<"${HTTP_BODY}")"
    VM="$(json_field name <<<"${HTTP_BODY}")"
    # The token response is never logged.
    local tmp
    tmp="$(mktemp)"
    HTTP_CODE="$(curl -sS --fail-with-body --max-time 10 -H 'Metadata:true' -o "${tmp}" -w '%{http_code}' \
        "${IMDS}/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fmanagement.azure.com%2F" 2>/dev/null)" || true
    if ! ok_code "${HTTP_CODE:-000}"; then
        rm -f "${tmp}"
        err "IMDS token failed (HTTP ${HTTP_CODE:-000})"
        return 1
    fi
    TOKEN="$(json_field access_token <"${tmp}" 2>/dev/null)" || {
        rm -f "${tmp}"
        err "IMDS token failed (unreadable response)"
        return 1
    }
    rm -f "${tmp}"
    VM_URL="${ARM}/subscriptions/${SUB}/resourceGroups/${RG}/providers/Microsoft.Compute/virtualMachines/${VM}"
}

deallocate_once() {
    get_identity || return 1
    http -X POST -H "Authorization: Bearer ${TOKEN}" -H 'Content-Length: 0' \
        "${VM_URL}/deallocate?api-version=${ARM_API_VERSION}"
    if ! ok_code "${HTTP_CODE}"; then
        err "ARM returned ${HTTP_CODE}: ${HTTP_BODY:0:500}"
        return 1
    fi
    log "deallocation requested for ${VM}"
}

check_once() {
    get_identity || return 1
    http -H "Authorization: Bearer ${TOKEN}" "${VM_URL}?api-version=${ARM_API_VERSION}"
    if ! ok_code "${HTTP_CODE}"; then
        err "ARM returned ${HTTP_CODE}: ${HTTP_BODY:0:500}"
        return 1
    fi
    log "check ok: IMDS token and ARM read work for ${VM}"
}

# retry <function>: up to ATTEMPTS tries with a growing pause.
retry() {
    local attempt
    for attempt in $(seq 1 "${ATTEMPTS}"); do
        if "$1"; then
            return 0
        fi
        err "attempt ${attempt} of ${ATTEMPTS} failed"
        if [ "${attempt}" -lt "${ATTEMPTS}" ]; then
            sleep $((RETRY_SLEEP * attempt))
        fi
    done
    return 1
}

now="${CFDAGENT_NOW:-$(date +%s)}"

# Print "active <age>" (a run is under way), "stale <age>" or "none" for the active-run marker.
# Only the modification time counts, never the content. A missing marker, something that is not a
# regular file (a directory, a dangling link), an unreadable time and a time in the future all
# count as "none": a damaged marker must not keep the VM running.
active_run_state() {
    local mtime age
    if [ ! -f "${ACTIVE_FILE}" ]; then
        echo none
        return 0
    fi
    mtime="$(stat -L -c %Y "${ACTIVE_FILE}" 2>/dev/null || true)"
    case "${mtime}" in
        '' | *[!0-9]*)
            echo none
            return 0
            ;;
    esac
    age=$((now - mtime))
    if [ "${age}" -lt 0 ]; then
        echo none
    elif [ "${age}" -lt "${ACTIVE_MAX_SECONDS}" ]; then
        echo "active ${age}"
    else
        echo "stale ${age}"
    fi
}

if [ "${MODE}" = check ]; then
    retry check_once
    exit $?
fi

if [ "${MODE}" = daily ]; then
    read -r marker_state marker_age <<<"$(active_run_state)"
    case "${marker_state}" in
        active)
            log "deferred: active run (marker is $((marker_age / 60)) of $((ACTIVE_MAX_SECONDS / 60)) minutes old); the next timer run asks again"
            exit 0
            ;;
        stale)
            log "ignoring stale active-run marker ($((marker_age / 60)) minutes old, limit $((ACTIVE_MAX_SECONDS / 60)))"
            ;;
    esac
fi

if [ "${MODE}" = force ] || [ "${MODE}" = daily ]; then
    if [ "${DRY}" = 1 ]; then
        log "dry run: would deallocate (${MODE} shutdown)"
        exit 0
    fi
    log "forced shutdown"
    wall "cfdagent: daily shutdown, this VM is being deallocated now" 2>/dev/null || true
    retry deallocate_once
    exit $?
fi

if reason="$(busy_reason)"; then
    rm -f "${IDLE_FILE}"
    log "busy (${reason}); idle counter reset"
    exit 0
fi

if [ -n "${CFDAGENT_UPTIME_SECONDS:-}" ]; then
    uptime_s="${CFDAGENT_UPTIME_SECONDS}"
else
    uptime_s="$(cut -d. -f1 /proc/uptime)"
fi
boot=$((now - uptime_s))

mkdir -p "${STATE_DIR}"
since="${now}"
if [ -s "${IDLE_FILE}" ]; then
    stored="$(head -n1 "${IDLE_FILE}")"
    case "${stored}" in
        '' | *[!0-9]*) ;; # corrupt: start counting now
        *) since="${stored}" ;;
    esac
fi
# A timestamp from before this boot belongs to an earlier session: count from boot.
if [ "${since}" -lt "${boot}" ]; then
    since="${boot}"
fi
echo "${since}" >"${IDLE_FILE}"

idle_s=$((now - since))
limit_s=$((IDLE_MINUTES * 60))
if [ "${idle_s}" -lt "${limit_s}" ]; then
    log "idle for $((idle_s / 60)) of ${IDLE_MINUTES} minutes; not yet"
    exit 0
fi

if [ "${DRY}" = 1 ]; then
    log "dry run: would deallocate (idle for $((idle_s / 60)) minutes)"
    exit 0
fi

log "idle for $((idle_s / 60)) minutes; deallocating"
retry deallocate_once
