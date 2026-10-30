#!/usr/bin/env bash
# Set up (or update) the cfd-agent host on the VM. Idempotent; run as root or through sudo.
#
# Usage: vm-host-setup.sh <repo-ref> --llm-endpoint URL --storage-account-url URL
#                         [--image TAG|REFERENCE] [--user NAME]
#                         [--shutdown-time HHMM] [--timezone ZONE]
#   <repo-ref>              branch, tag or commit of https://github.com/mkrajew/cfd-agent to install.
#   --llm-endpoint          OpenAI-compatible base URL of the AI account (output `aiEndpoint`).
#   --storage-account-url   Blob endpoint, https://<storageAccountName>.blob.core.windows.net.
#   --image                 image tag (repository ghcr.io/mkrajew/cfd-agent) or a full reference.
#                           Default: sha-<first 7 characters> when <repo-ref> is a commit id,
#                           otherwise latest.
#   --user                  VM administrator that owns the runs directory and may log in (azureuser).
#   --shutdown-time/--timezone  daily shutdown, default 2300 Europe/Warsaw (as in main.bicep).
#   --render env|timer|ssh  print the rendered file and exit; changes nothing, needs no root.
#
# What it does:
#   1. installs git and python3-venv (apt);
#   2. clones the repository to /opt/cfdagent-src (fetch and checkout when it exists);
#   3. builds /opt/cfdagent-host (venv) and pip-installs the repository there, host dependencies only
#      (no toolchain extra: CadQuery, Gmsh and OpenFOAM live in the image);
#   4. writes /etc/cfdagent/env and creates the runs and state directories;
#   5. installs the /usr/local/bin/cfdagent wrapper, which loads that env file;
#   6. installs the scripts and units from the checkout: idle shutdown, daily shutdown with
#      deferral, and the metadata firewall (enabled and started);
#   7. installs the sshd drop-in; `sshd -t` must accept it before sshd is reloaded;
#   8. pulls the image.
# No secret is read, written or printed: the values are endpoints and an image name.
# Exit status: 0 success, 1 error, 2 usage error, 3 everything done but the image pull failed.
set -euo pipefail

REPO_URL="${CFDAGENT_SETUP_REPO_URL:-https://github.com/mkrajew/cfd-agent}"
IMAGE_REPO="ghcr.io/mkrajew/cfd-agent"
SRC_DIR="${CFDAGENT_SETUP_SRC_DIR:-/opt/cfdagent-src}"
HOST_DIR="${CFDAGENT_SETUP_HOST_DIR:-/opt/cfdagent-host}"
ENV_DIR="${CFDAGENT_SETUP_ENV_DIR:-/etc/cfdagent}"
BIN_DIR="${CFDAGENT_SETUP_BIN_DIR:-/usr/local/bin}"
SBIN_DIR="${CFDAGENT_SETUP_SBIN_DIR:-/usr/local/sbin}"
UNIT_DIR="${CFDAGENT_SETUP_UNIT_DIR:-/etc/systemd/system}"
SSHD_DIR="${CFDAGENT_SETUP_SSHD_DIR:-/etc/ssh/sshd_config.d}"
RUN_SSHD_DIR="${CFDAGENT_SETUP_RUN_SSHD_DIR:-/run/sshd}"
STATE_DIR="/var/lib/cfdagent"
SSH_DROPIN="20-cfdagent-ssh-extra.conf"
DAILY_ATTEMPTS=25 # the shutdown time, then every 10 minutes for 4 hours

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REPO_REF=
LLM_ENDPOINT=
STORAGE_URL=
IMAGE_ARG=
ADMIN_USER=azureuser
SHUTDOWN_TIME=2300
TIMEZONE=Europe/Warsaw
RENDER=
PULL_FAILED=0
COMMIT=

usage() {
    sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed -e '/^set -euo/d' -e 's/^# \{0,1\}//' >&2
}

die() {
    echo "vm-host-setup: $*" >&2
    exit 1
}

usage_error() {
    echo "vm-host-setup: $*" >&2
    echo "usage: vm-host-setup.sh <repo-ref> --llm-endpoint URL --storage-account-url URL" \
        "[--image TAG] [--user NAME] [--shutdown-time HHMM] [--timezone ZONE]" >&2
    exit 2
}

info() { echo "==> $*"; }

parse_args() {
    [ "$#" -ge 1 ] || usage_error "missing <repo-ref>"
    case "$1" in
        -h | --help)
            usage
            exit 0
            ;;
        -*) usage_error "the first argument must be the repository ref, not an option" ;;
    esac
    REPO_REF="$1"
    shift
    while [ "$#" -gt 0 ]; do
        [ "$#" -ge 2 ] || usage_error "option $1 needs a value"
        case "$1" in
            --llm-endpoint) LLM_ENDPOINT="$2" ;;
            --storage-account-url) STORAGE_URL="$2" ;;
            --image) IMAGE_ARG="$2" ;;
            --user) ADMIN_USER="$2" ;;
            --shutdown-time) SHUTDOWN_TIME="$2" ;;
            --timezone) TIMEZONE="$2" ;;
            --render) RENDER="$2" ;;
            *) usage_error "unknown option $1" ;;
        esac
        shift 2
    done
    [ -n "${LLM_ENDPOINT}" ] || usage_error "--llm-endpoint is required"
    [ -n "${STORAGE_URL}" ] || usage_error "--storage-account-url is required"
    # Values end up unquoted in an env file that bash sources, so keep them to a safe alphabet.
    [[ "${REPO_REF}" =~ ^[A-Za-z0-9._/+-]+$ ]] || usage_error "invalid <repo-ref>"
    [[ "${LLM_ENDPOINT}" =~ ^https://[A-Za-z0-9._:/-]+$ ]] || usage_error "--llm-endpoint must be an https URL"
    [[ "${STORAGE_URL}" =~ ^https://[A-Za-z0-9._:/-]+$ ]] || usage_error "--storage-account-url must be an https URL"
    [[ "${ADMIN_USER}" =~ ^[a-z_][a-z0-9_-]*$ ]] || usage_error "invalid --user"
    [[ "${SHUTDOWN_TIME}" =~ ^([01][0-9]|2[0-3])[0-5][0-9]$ ]] || usage_error "--shutdown-time must be HHMM"
    [[ "${TIMEZONE}" =~ ^[A-Za-z0-9_/+-]+$ ]] || usage_error "invalid --timezone"
    if [ -n "${IMAGE_ARG}" ]; then
        [[ "${IMAGE_ARG}" =~ ^[A-Za-z0-9._:/@-]+$ ]] || usage_error "invalid --image"
    fi
    case "${RENDER}" in
        '' | env | timer | ssh) ;;
        *) usage_error "--render takes env, timer or ssh" ;;
    esac
}

# Directory with the files of infra/vm: the checkout on the VM, the working tree when run from one.
vm_files_dir() {
    if [ -n "${CFDAGENT_SETUP_VM_FILES_DIR:-}" ]; then
        echo "${CFDAGENT_SETUP_VM_FILES_DIR}"
    elif [ -d "${SRC_DIR}/infra/vm" ]; then
        echo "${SRC_DIR}/infra/vm"
    else
        echo "${SCRIPT_DIR}/../infra/vm"
    fi
}

image_reference() {
    if [ -n "${IMAGE_ARG}" ]; then
        case "${IMAGE_ARG}" in
            */* | *:*) echo "${IMAGE_ARG}" ;;
            *) echo "${IMAGE_REPO}:${IMAGE_ARG}" ;;
        esac
    elif [[ "${REPO_REF}" =~ ^[0-9a-f]{7,40}$ ]]; then
        echo "${IMAGE_REPO}:sha-${REPO_REF:0:7}"
    else
        echo "${IMAGE_REPO}:latest"
    fi
}

home_dir() {
    local home
    home="$(getent passwd "${ADMIN_USER}" 2>/dev/null | cut -d: -f6 || true)"
    echo "${home:-/home/${ADMIN_USER}}"
}

render_env() {
    cat <<EOF
# Written by scripts/vm-host-setup.sh; read by /usr/local/bin/cfdagent. No secrets: access to the
# model and to Blob storage uses the VM's managed identity.
CFDAGENT_LLM_ENDPOINT=${LLM_ENDPOINT}
CFDAGENT_STORAGE_ACCOUNT_URL=${STORAGE_URL}
CFDAGENT_RUNS_DIR=$(home_dir)/runs
CFDAGENT_IMAGE=$(image_reference)
CFDAGENT_STATE_DIR=${STATE_DIR}
EOF
}

# The daily timer: one OnCalendar line per attempt (see infra/vm/daily-shutdown.timer).
render_timer() {
    local start step minutes entries='' entry template
    start=$((10#${SHUTDOWN_TIME:0:2} * 60 + 10#${SHUTDOWN_TIME:2:2}))
    for ((step = 0; step < DAILY_ATTEMPTS; step++)); do
        minutes=$(((start + step * 10) % 1440))
        printf -v entry '*-*-* %02d:%02d:00 %s' $((minutes / 60)) $((minutes % 60)) "${TIMEZONE}"
        if [ -z "${entries}" ]; then
            entries="${entry}"
        else
            entries+=$'\nOnCalendar='"${entry}"
        fi
    done
    template="$(cat "$(vm_files_dir)/daily-shutdown.timer")"
    printf '%s\n' "${template//__ON_CALENDAR__/${entries}}"
}

render_ssh() {
    local template
    template="$(cat "$(vm_files_dir)/${SSH_DROPIN}")"
    printf '%s\n' "${template//__ADMIN_USER__/${ADMIN_USER}}"
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
    id "${ADMIN_USER}" >/dev/null 2>&1 || die "user ${ADMIN_USER} does not exist (use --user)"
}

install_packages() {
    info "packages"
    local missing=()
    command -v git >/dev/null 2>&1 || missing+=(git)
    dpkg-query -W -f='${Status}' python3-venv 2>/dev/null | grep -q 'ok installed' || missing+=(python3-venv)
    if [ "${#missing[@]}" -gt 0 ]; then
        DEBIAN_FRONTEND=noninteractive apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}"
    fi
}

sync_source() {
    info "source ${REPO_REF} -> ${SRC_DIR}"
    if [ ! -d "${SRC_DIR}/.git" ]; then
        git clone --quiet "${REPO_URL}" "${SRC_DIR}"
    fi
    git -C "${SRC_DIR}" remote set-url origin "${REPO_URL}"
    git -C "${SRC_DIR}" fetch --quiet --tags --prune --force origin \
        '+refs/heads/*:refs/remotes/origin/*'
    local commit
    # A branch name means the remote branch (a local branch of an earlier clone may be stale).
    commit="$(git -C "${SRC_DIR}" rev-parse --verify --quiet "origin/${REPO_REF}^{commit}" ||
        git -C "${SRC_DIR}" rev-parse --verify --quiet "${REPO_REF}^{commit}" || true)"
    [ -n "${commit}" ] || die "ref ${REPO_REF} not found in ${REPO_URL}"
    git -C "${SRC_DIR}" checkout --quiet --force --detach "${commit}"
    COMMIT="${commit}"
}

build_venv() {
    info "host environment ${HOST_DIR}"
    [ -x "${HOST_DIR}/bin/python" ] || python3 -m venv "${HOST_DIR}"
    "${HOST_DIR}/bin/pip" install --quiet --upgrade "${SRC_DIR}"
    # The version string stays the same between commits, so make sure our own code is replaced.
    "${HOST_DIR}/bin/pip" install --quiet --force-reinstall --no-deps "${SRC_DIR}"
}

write_env() {
    info "environment file ${ENV_DIR}/env"
    local group tmp
    group="$(id -gn "${ADMIN_USER}")"
    install -d -m 0750 -o root -g "${group}" "${ENV_DIR}"
    tmp="$(mktemp "${ENV_DIR}/env.XXXXXX")"
    render_env >"${tmp}"
    chown "root:${group}" "${tmp}"
    chmod 0640 "${tmp}"
    mv -f "${tmp}" "${ENV_DIR}/env"
}

make_dirs() {
    local group
    group="$(id -gn "${ADMIN_USER}")"
    install -d -m 0755 -o "${ADMIN_USER}" -g "${group}" "$(home_dir)/runs" "${STATE_DIR}"
}

install_wrapper() {
    info "wrapper ${BIN_DIR}/cfdagent"
    cat >"${BIN_DIR}/cfdagent.new" <<EOF
#!/usr/bin/env bash
# Written by scripts/vm-host-setup.sh: load the host configuration, then run the CLI.
set -a
. ${ENV_DIR}/env
set +a
exec ${HOST_DIR}/bin/cfdagent "\$@"
EOF
    chmod 0755 "${BIN_DIR}/cfdagent.new"
    mv -f "${BIN_DIR}/cfdagent.new" "${BIN_DIR}/cfdagent"
    if [ ! -x "${HOST_DIR}/bin/cfdagent" ]; then
        echo "note: ${HOST_DIR}/bin/cfdagent does not exist at this commit (no console script yet);" \
            "run this script again with a later ref" >&2
    fi
}

install_vm_units() {
    info "scripts and units"
    local files
    files="$(vm_files_dir)"
    install -d -m 0755 "${SBIN_DIR}" "${UNIT_DIR}"
    install -m 0755 "${files}/idle-shutdown.sh" "${SBIN_DIR}/cfdagent-idle-shutdown"
    install -m 0755 "${files}/cfdagent-iptables.sh" "${SBIN_DIR}/cfdagent-iptables"
    install -m 0644 "${files}/idle-shutdown.service" "${UNIT_DIR}/cfdagent-idle-shutdown.service"
    install -m 0644 "${files}/idle-shutdown.timer" "${UNIT_DIR}/cfdagent-idle-shutdown.timer"
    install -m 0644 "${files}/daily-shutdown.service" "${UNIT_DIR}/cfdagent-daily-shutdown.service"
    render_timer >"${UNIT_DIR}/cfdagent-daily-shutdown.timer"
    chmod 0644 "${UNIT_DIR}/cfdagent-daily-shutdown.timer"
    install -m 0644 "${files}/cfdagent-iptables.service" "${UNIT_DIR}/cfdagent-iptables.service"
    systemctl daemon-reload
    systemctl enable cfdagent-iptables.service
    systemctl restart cfdagent-iptables.service
    systemctl enable --now cfdagent-idle-shutdown.timer
    systemctl enable --now cfdagent-daily-shutdown.timer
    systemctl restart cfdagent-daily-shutdown.timer
}

# Install the sshd drop-in only if `sshd -t` accepts the configuration with it; otherwise put the
# previous drop-in back (or remove ours) and do not reload.
install_ssh_dropin() {
    info "sshd drop-in ${SSHD_DIR}/${SSH_DROPIN}"
    local target="${SSHD_DIR}/${SSH_DROPIN}"
    local fresh="${target}.new" previous="${target}.prev"
    install -d -m 0755 "${SSHD_DIR}" "${RUN_SSHD_DIR}"
    # The .new and .prev names do not match sshd's *.conf include, so they are inert.
    render_ssh >"${fresh}"
    chmod 0644 "${fresh}"
    rm -f "${previous}"
    if [ -f "${target}" ]; then
        cp -p "${target}" "${previous}"
    fi
    mv -f "${fresh}" "${target}"
    if ! sshd -t; then
        if [ -f "${previous}" ]; then
            mv -f "${previous}" "${target}"
        else
            rm -f "${target}"
        fi
        die "sshd -t failed with the new drop-in; the previous state is restored, sshd was not reloaded"
    fi
    rm -f "${previous}"
    systemctl reload ssh
}

pull_image() {
    local image
    image="$(image_reference)"
    info "image ${image}"
    if ! docker pull "${image}"; then
        PULL_FAILED=1
        echo "warning: could not pull ${image} (not published or not public yet)" >&2
    fi
}

summary() {
    echo
    echo "cfd-agent host is set up"
    echo "  repository   ${REPO_REF} (${COMMIT:-unknown}) in ${SRC_DIR}"
    echo "  environment  ${HOST_DIR}"
    echo "  config       ${ENV_DIR}/env"
    sed -e '/^#/d' -e 's/^/    /' "${ENV_DIR}/env"
    echo "  admin user   ${ADMIN_USER} (the only SSH user, MaxAuthTries 3)"
    echo "  daily        ${SHUTDOWN_TIME} ${TIMEZONE}, then every 10 minutes for 4 hours while a run defers it"
    if [ "${PULL_FAILED}" = 1 ]; then
        echo "  image        NOT PULLED; fix the tag or publish the image, then run this script again"
    else
        echo "  image        pulled"
    fi
    echo
    echo "next: cfdagent-iptables status; systemctl list-timers 'cfdagent-*'; see docs/runbooks/vm-host.md"
}

main() {
    parse_args "$@"
    case "${RENDER}" in
        env)
            render_env
            return 0
            ;;
        timer)
            render_timer
            return 0
            ;;
        ssh)
            render_ssh
            return 0
            ;;
    esac
    require_root
    install_packages
    sync_source
    build_venv
    write_env
    make_dirs
    install_wrapper
    install_vm_units
    install_ssh_dropin
    pull_image
    summary
    [ "${PULL_FAILED}" = 0 ] || return 3
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
