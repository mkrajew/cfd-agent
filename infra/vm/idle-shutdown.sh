#!/usr/bin/env bash
# Deallocate this VM after 30 consecutive idle minutes.
#
# Run by cfdagent-idle-shutdown.timer every 5 minutes. The VM is busy when any of these holds:
#   - a login session exists (who),
#   - an established SSH connection exists (covers Remote-SSH sessions that have no tty),
#   - a cfdagent process is running (also inside a container),
#   - a container from the cfd-agent image is running.
# The idle start time lives in $STATE_DIR/idle-since. Deallocation uses the ARM REST API with the
# token of the VM's managed identity (IMDS); the identity may deallocate only this VM.
#
# Usage: cfdagent-idle-shutdown [--dry-run | --force]
#   --dry-run  take the decision and print it, but do not call Azure.
#   --force    deallocate now, whatever is running (used by the daily-shutdown timer).
# Test hooks (environment): CFDAGENT_STATE_DIR, CFDAGENT_IDLE_MINUTES, CFDAGENT_IMAGE,
#   CFDAGENT_NOW (epoch seconds), CFDAGENT_UPTIME_SECONDS.
set -euo pipefail

STATE_DIR="${CFDAGENT_STATE_DIR:-/var/lib/cfdagent}"
IDLE_MINUTES="${CFDAGENT_IDLE_MINUTES:-30}"
IMAGE_REPO="${CFDAGENT_IMAGE:-ghcr.io/mkrajew/cfd-agent}"
IDLE_FILE="${STATE_DIR}/idle-since"
IMDS="http://169.254.169.254/metadata"
ARM_API_VERSION="2024-07-01"

DRY_RUN=0
FORCE=0
case "${1:-}" in
    --dry-run) DRY_RUN=1 ;;
    --force) FORCE=1 ;;
    "") ;;
    *)
        echo "usage: $0 [--dry-run | --force]" >&2
        exit 2
        ;;
esac

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
    if pgrep -x cfdagent >/dev/null 2>&1 || pgrep -f -- '-m cfdagent( |$)' >/dev/null 2>&1; then
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

deallocate() {
    local meta sub rg vm token
    meta="$(curl -fsS --max-time 10 -H 'Metadata:true' \
        "${IMDS}/instance/compute?api-version=2021-02-01&format=json")"
    sub="$(json_field subscriptionId <<<"${meta}")"
    rg="$(json_field resourceGroupName <<<"${meta}")"
    vm="$(json_field name <<<"${meta}")"
    token="$(curl -fsS --max-time 10 -H 'Metadata:true' \
        "${IMDS}/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fmanagement.azure.com%2F" |
        json_field access_token)"
    curl -fsS --max-time 30 -X POST \
        -H "Authorization: Bearer ${token}" -H 'Content-Length: 0' \
        "https://management.azure.com/subscriptions/${sub}/resourceGroups/${rg}/providers/Microsoft.Compute/virtualMachines/${vm}/deallocate?api-version=${ARM_API_VERSION}" \
        >/dev/null
    echo "deallocation requested for ${vm}"
}

now="${CFDAGENT_NOW:-$(date +%s)}"

if [ "${FORCE}" -eq 1 ]; then
    echo "forced shutdown"
    wall "cfdagent: daily shutdown, this VM is being deallocated now" 2>/dev/null || true
    deallocate
    exit 0
fi

if reason="$(busy_reason)"; then
    rm -f "${IDLE_FILE}"
    echo "busy (${reason}); idle counter reset"
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
    echo "idle for $((idle_s / 60)) of ${IDLE_MINUTES} minutes; not yet"
    exit 0
fi

if [ "${DRY_RUN}" -eq 1 ]; then
    echo "dry run: would deallocate (idle for $((idle_s / 60)) minutes)"
    exit 0
fi

echo "idle for $((idle_s / 60)) minutes; deallocating"
deallocate
