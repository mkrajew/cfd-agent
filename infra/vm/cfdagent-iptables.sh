#!/usr/bin/env bash
# Block the Azure metadata endpoints for every container on the Docker bridge networks.
#
# Installed as /usr/local/sbin/cfdagent-iptables and run by cfdagent-iptables.service. The rules sit
# at the top of the DOCKER-USER chain, which Docker evaluates for all traffic that is forwarded from
# or to a container, before its own rules:
#   1. accept UDP and TCP port 53 to 168.63.129.16 (the Azure DNS resolver on the WireServer address)
#   2. drop everything else to 168.63.129.16 (WireServer: extension and agent channel)
#   3. drop everything to 169.254.169.254 (IMDS: the VM identity token)
# The host itself (the orchestrator, the idle-shutdown script) is not affected: its traffic does not
# pass through FORWARD. Executor containers also run with --network none; this is the second layer.
#
# Usage: cfdagent-iptables start | stop | status
#   start   remove our rules, then insert them in the order above (idempotent; every insert is
#           preceded by a `-C` check). Fails when the DOCKER-USER chain does not exist.
#   stop    remove our rules; rules of other origin are left alone.
#   status  exit 0 when all four rules are in place.
set -euo pipefail

CHAIN=DOCKER-USER
# One rule per entry, in the order in which they must appear. The spelling is the one that
# `iptables -S` prints, so `-C` matches what an earlier run inserted.
RULES=(
    "-d 168.63.129.16/32 -p udp -m udp --dport 53 -j ACCEPT"
    "-d 168.63.129.16/32 -p tcp -m tcp --dport 53 -j ACCEPT"
    "-d 168.63.129.16/32 -j DROP"
    "-d 169.254.169.254/32 -j DROP"
)

# The rule words are deliberately unquoted: iptables takes them as separate arguments.
# shellcheck disable=SC2086
has_rule() { iptables -w -C "${CHAIN}" $1 2>/dev/null; }
# shellcheck disable=SC2086
insert_rule() { iptables -w -I "${CHAIN}" "$1" $2; }
# shellcheck disable=SC2086
delete_rule() { iptables -w -D "${CHAIN}" $1; }

remove_all() {
    local rule
    for rule in "${RULES[@]}"; do
        while has_rule "${rule}"; do
            delete_rule "${rule}"
        done
    done
}

action="${1:-}"
case "${action}" in
    start)
        if ! iptables -w -L "${CHAIN}" -n >/dev/null 2>&1; then
            echo "cfdagent-iptables: chain ${CHAIN} does not exist; is docker.service running?" >&2
            exit 1
        fi
        remove_all
        position=1
        for rule in "${RULES[@]}"; do
            if ! has_rule "${rule}"; then
                insert_rule "${position}" "${rule}"
            fi
            position=$((position + 1))
        done
        echo "cfdagent-iptables: metadata rules in place in ${CHAIN}"
        ;;
    stop)
        if iptables -w -L "${CHAIN}" -n >/dev/null 2>&1; then
            remove_all
        fi
        echo "cfdagent-iptables: metadata rules removed from ${CHAIN}"
        ;;
    status)
        for rule in "${RULES[@]}"; do
            if ! has_rule "${rule}"; then
                echo "missing: ${rule}" >&2
                exit 1
            fi
        done
        echo "all metadata rules present in ${CHAIN}"
        ;;
    *)
        echo "usage: $0 start | stop | status" >&2
        exit 2
        ;;
esac
