#!/bin/bash
# Entrypoint: run the given command inside the OpenFOAM ESI environment.
# Used for `docker run <image> <cmd>`; interactive shells get the same environment from
# /etc/bash.bashrc, and `bash -c` shells (docker exec) from BASH_ENV (docker/openfoam-env.sh).

# `docker run --user <uid>` can give an unknown uid with an unwritable HOME; use a scratch one.
if [ ! -w "${HOME:-/nonexistent}" ]; then
    HOME="/tmp/home-$(id -u)"
    export HOME
    mkdir -p "${HOME}"
fi

# shellcheck disable=SC1091
. /etc/cfd/openfoam-env.sh
if [ -z "${WM_PROJECT_VERSION:-}" ]; then
    echo "cfd-entrypoint: the OpenFOAM environment could not be loaded" >&2
    exit 1
fi

exec "$@"
