#!/usr/bin/env bash
# Set up (or update) the cfd-agent host on the VM. Idempotent; run as root or through sudo.
#
# Usage: vm-host-setup.sh <repo-ref> --llm-endpoint URL --storage-account-url URL
#                         [--image TAG|REFERENCE] [--user NAME]
#                         [--shutdown-time HHMM] [--timezone ZONE] [--force-while-running]
#   <repo-ref>              commit (preferred), tag or branch of https://github.com/mkrajew/cfd-agent.
#                           It must contain infra/vm/ with the VM host files. The pip build runs the
#                           repository's build backend as root, so the ref must be one you trust.
#   --llm-endpoint          OpenAI-compatible base URL of the AI account (output `aiEndpoint`).
#   --storage-account-url   Blob endpoint, https://<storageAccountName>.blob.core.windows.net.
#   --image                 image tag (repository ghcr.io/mkrajew/cfd-agent) or a full reference.
#                           Default: sha-<first 7 characters of the installed commit>. The pull fails
#                           loudly when that image is not published.
#   --user                  VM administrator that owns the runs directory and may log in (azureuser).
#   --shutdown-time/--timezone  daily shutdown, default 2300 Europe/Warsaw (as in main.bicep).
#   --force-while-running   run although /var/lib/cfdagent/active-run is younger than 4 hours
#                           (a `cfdagent run` is under way; the default is to refuse).
#   --render env|timer|ssh  print the rendered file and exit; changes nothing, needs no root.
#
# What it does:
#   1. refuses to run while a fresh active-run marker exists (see --force-while-running);
#   2. installs git and python3-venv (apt), clones or updates /opt/cfdagent-src and checks that the
#      checkout has every infra/vm file, before anything else is changed;
#   3. creates the runs and state directories, installs the scripts and units from the checkout (idle
#      shutdown, daily shutdown with deferral, metadata firewall enabled and started) and the sshd
#      drop-in; `sshd -t` must accept it before sshd is reloaded;
#   4. builds /opt/cfdagent-host (venv) and pip-installs the repository there, host dependencies only
#      (no toolchain extra: CadQuery, Gmsh and OpenFOAM live in the image);
#   5. writes /etc/cfdagent/env and the /usr/local/bin/cfdagent wrapper that loads it;
#   6. pulls the image.
# The security items come first, so a failure of the network-dependent steps (PyPI, GHCR) leaves them
# in place; run the script again after such a failure.
# No secret is read, written or printed: the values are endpoints and an image name.
# Exit status: 0 success, 1 error, 2 usage error, 3 everything done but the image pull failed.
# Started with `sh script`, the script re-executes itself with bash.
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"
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
STATE_DIR="${CFDAGENT_SETUP_STATE_DIR:-/var/lib/cfdagent}"
ENV_OWNER="${CFDAGENT_SETUP_ENV_OWNER:-root}"
SSH_DROPIN="20-cfdagent-ssh-extra.conf"
ACTIVE_MAX_SECONDS=$((4 * 3600))
# Files of infra/vm that this script installs; the checkout must contain all of them.
REQUIRED_VM_FILES=(
    idle-shutdown.sh
    idle-shutdown.service
    idle-shutdown.timer
    daily-shutdown.service
    daily-shutdown.timer
    cfdagent-iptables.sh
    cfdagent-iptables.service
    "${SSH_DROPIN}"
)

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
FORCE_WHILE_RUNNING=0
COMMIT=

usage() {
    sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | grep '^#' | sed -e 's/^# \{0,1\}//' >&2
}

die() {
    echo "vm-host-setup: $*" >&2
    exit 1
}

usage_error() {
    echo "vm-host-setup: $*" >&2
    echo "usage: vm-host-setup.sh <repo-ref> --llm-endpoint URL --storage-account-url URL" \
        "[--image TAG] [--user NAME] [--shutdown-time HHMM] [--timezone ZONE]" \
        "[--force-while-running]" >&2
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
        if [ "$1" = --force-while-running ]; then
            FORCE_WHILE_RUNNING=1
            shift
            continue
        fi
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
    elif [ -n "${COMMIT}" ]; then
        echo "${IMAGE_REPO}:sha-${COMMIT:0:7}"
    elif [[ "${REPO_REF}" =~ ^[0-9a-f]{7,40}$ ]]; then
        # --render only: nothing is checked out, so a commit id in the ref stands in for it.
        echo "${IMAGE_REPO}:sha-${REPO_REF:0:7}"
    else
        # --render only: a real run resolves a branch or tag to a commit first.
        echo "${IMAGE_REPO}:latest"
    fi
}

home_dir() {
    local home
    if [ -n "${CFDAGENT_SETUP_HOME_DIR:-}" ]; then
        echo "${CFDAGENT_SETUP_HOME_DIR}"
        return 0
    fi
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
    if [ -n "${COMMIT}" ]; then
        echo "CFDAGENT_COMMIT=${COMMIT}"
    fi
}

# The OnCalendar value of the daily shutdown, as vm.bicep builds it for a new VM.
daily_calendar() {
    echo "*-*-* ${SHUTDOWN_TIME:0:2}:${SHUTDOWN_TIME:2:2}:00 ${TIMEZONE}"
}

# systemd-analyze, when present, refuses a calendar expression that systemd would reject.
validate_calendar() {
    if command -v systemd-analyze >/dev/null 2>&1; then
        systemd-analyze calendar "$1" >/dev/null 2>&1 || die "systemd rejects the calendar expression '$1'"
    fi
}

render_timer() {
    local template
    template="$(cat "$(vm_files_dir)/daily-shutdown.timer")"
    printf '%s\n' "${template//__ON_CALENDAR__/$(daily_calendar)}"
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

# True when a `cfdagent run` is under way: a regular active-run marker younger than 4 hours.
active_run_is_fresh() {
    local marker="${STATE_DIR}/active-run" mtime age
    [ -f "${marker}" ] || return 1
    mtime="$(stat -L -c %Y "${marker}" 2>/dev/null || true)"
    case "${mtime}" in '' | *[!0-9]*) return 1 ;; esac
    age=$(($(date +%s) - mtime))
    [ "${age}" -ge 0 ] && [ "${age}" -lt "${ACTIVE_MAX_SECONDS}" ]
}

refuse_while_run_active() {
    if [ "${FORCE_WHILE_RUNNING}" != 1 ] && active_run_is_fresh; then
        die "a run is active (${STATE_DIR}/active-run is younger than 4 hours); wait for it or use --force-while-running"
    fi
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
    # A private or missing repository must fail, not wait for a password on a terminal.
    export GIT_TERMINAL_PROMPT=0
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

# Every infra/vm file must exist in the checkout: an older ref would otherwise fail halfway.
check_vm_files() {
    local files name
    files="$(vm_files_dir)"
    for name in "${REQUIRED_VM_FILES[@]}"; do
        [ -f "${files}/${name}" ] ||
            die "ref ${REPO_REF} does not contain infra/vm/${name}; use a ref that includes the VM host files"
    done
}

build_venv() {
    info "host environment ${HOST_DIR}"
    # bin/pip, not bin/python: an interrupted `python3 -m venv` leaves a python without pip.
    [ -x "${HOST_DIR}/bin/pip" ] || python3 -m venv "${HOST_DIR}"
    "${HOST_DIR}/bin/pip" install --quiet --upgrade "${SRC_DIR}"
    # The version string stays the same between commits, so make sure our own code is replaced.
    "${HOST_DIR}/bin/pip" install --quiet --force-reinstall --no-deps "${SRC_DIR}"
}

write_env() {
    info "environment file ${ENV_DIR}/env"
    local group tmp
    group="$(id -gn "${ADMIN_USER}")"
    install -d -m 0750 -o "${ENV_OWNER}" -g "${group}" "${ENV_DIR}"
    tmp="$(mktemp "${ENV_DIR}/env.XXXXXX")"
    render_env >"${tmp}"
    chown "${ENV_OWNER}:${group}" "${tmp}"
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
    local files timer_tmp
    files="$(vm_files_dir)"
    install -d -m 0755 "${SBIN_DIR}" "${UNIT_DIR}"
    # Render and validate the timer before anything is installed; move it into place afterwards.
    validate_calendar "$(daily_calendar)"
    timer_tmp="$(mktemp "${UNIT_DIR}/.cfdagent-daily-timer.XXXXXX")"
    render_timer >"${timer_tmp}"
    chmod 0644 "${timer_tmp}"
    install -m 0755 "${files}/idle-shutdown.sh" "${SBIN_DIR}/cfdagent-idle-shutdown"
    install -m 0755 "${files}/cfdagent-iptables.sh" "${SBIN_DIR}/cfdagent-iptables"
    install -m 0644 "${files}/idle-shutdown.service" "${UNIT_DIR}/cfdagent-idle-shutdown.service"
    install -m 0644 "${files}/idle-shutdown.timer" "${UNIT_DIR}/cfdagent-idle-shutdown.timer"
    install -m 0644 "${files}/daily-shutdown.service" "${UNIT_DIR}/cfdagent-daily-shutdown.service"
    mv -f "${timer_tmp}" "${UNIT_DIR}/cfdagent-daily-shutdown.timer"
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
    # try-reload-or-restart does nothing (and succeeds) while ssh.service is inactive, which can
    # happen with socket activation; a plain reload would fail and abort the script.
    systemctl try-reload-or-restart ssh
}

pull_image() {
    local image
    image="$(image_reference)"
    info "image ${image}"
    if ! docker pull "${image}"; then
        PULL_FAILED=1
        echo "ERROR: could not pull ${image}: the image for commit ${COMMIT:-unknown} is not published" \
            "(images are built for main and release tags) or is not public. Pass --image <tag> to use" \
            "another one, or run this script again once it is published." >&2
    fi
}

summary() {
    echo
    echo "cfd-agent host is set up"
    echo "  repository   ${REPO_REF}, commit ${COMMIT:-unknown}, in ${SRC_DIR}"
    echo "  environment  ${HOST_DIR}"
    echo "  config       ${ENV_DIR}/env"
    sed -e '/^#/d' -e 's/^/    /' "${ENV_DIR}/env"
    echo "  admin user   ${ADMIN_USER} (the only SSH user, MaxAuthTries 3)"
    echo "  daily        ${SHUTDOWN_TIME} ${TIMEZONE}, then every 10 minutes while the VM stays up (deferred during a run)"
    if [ "${PULL_FAILED}" = 1 ]; then
        echo "  image        NOT PULLED; see the error above, then run this script again"
    else
        echo "  image        pulled"
    fi
    echo
    echo "next: cfdagent-iptables status; systemctl list-timers 'cfdagent-*'; see docs/runbooks/vm-host.md"
}

finish() {
    summary
    [ "${PULL_FAILED}" = 0 ] || return 3
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
    refuse_while_run_active
    install_packages
    sync_source
    check_vm_files
    make_dirs
    install_vm_units
    install_ssh_dropin
    build_venv
    write_env
    install_wrapper
    pull_image
    finish
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
