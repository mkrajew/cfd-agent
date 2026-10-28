# shellcheck shell=bash
# Source the OpenFOAM ESI environment once. Sourced by the entrypoint and, through BASH_ENV, by
# non-interactive bash shells (for example `docker exec <container> bash -c 'icoFoam -help'`).
# The profile script comes from the official OpenFOAM image: it sources etc/bashrc of the
# installed release and sets the OpenMPI container workaround.
if [ -z "${WM_PROJECT_VERSION:-}" ] && [ -f /etc/profile.d/openfoam-99run.sh ]; then
    # shellcheck disable=SC1091
    . /etc/profile.d/openfoam-99run.sh || true
fi
