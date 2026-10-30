# syntax=docker/dockerfile:1
#
# cfdagent - OpenFOAM ESI + Gmsh + CadQuery + PyVista + the cfdagent package.
#
# Two targets share one hardened base:
#   test     the development image: runtime plus pytest, ruff and pre-commit (integration tests)
#   runtime  the image that is published and that executor containers run: no uv, no pytest, no
#            pre-commit, no compilers, no setuid binaries, OpenFOAM refuses case-supplied code
# The last stage is `runtime`, so a plain `docker build` produces the runtime image.
#
# Build (from the repository root):
#   docker build -f docker/Dockerfile --target test -t cfdagent:dev .
#   docker build -f docker/Dockerfile --target runtime -t cfdagent:runtime .
# Integration tests (repo bind-mounted at /work): see "Key commands" in CLAUDE.md.

# uv, pinned by tag and digest. A stage of its own so Dependabot can update it.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

# OpenFOAM ESI v2512 (openfoam.com), the latest release with an official image on Docker Hub
# (opencfd/openfoam-default). Ubuntu 24.04, linux/amd64 + linux/arm64. Pinned by tag and digest.
FROM opencfd/openfoam-default:2512@sha256:33fb575aa9980d2bc42fd58c75ae698c489293ba30c991380fe3f899c622f319 AS openfoam

# --------------------------------------------------------------------------------------------
# Python environments. Built here, with uv and the compilers of the base image, and copied into
# the final stages, which therefore contain neither.
# --------------------------------------------------------------------------------------------
FROM openfoam AS python-base
USER root
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_PYTHON=3.12 \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1
WORKDIR /opt/build
COPY pyproject.toml uv.lock ./
COPY README.md LICENSE ./
COPY src ./src

# The runtime environment: the locked dependencies without the dev group, and the package itself
# (non-editable).
FROM python-base AS venv-runtime
RUN uv sync --locked --extra toolchain --no-dev --no-install-project \
    && uv sync --locked --extra toolchain --no-dev --no-editable

# The test environment: the same plus the dev group (pytest, ruff, pre-commit).
FROM python-base AS venv-test
RUN uv sync --locked --extra toolchain --group dev --no-install-project \
    && uv sync --locked --extra toolchain --group dev --no-editable

# --------------------------------------------------------------------------------------------
# The hardened base: OpenFOAM and the system libraries, without a Python environment.
# --------------------------------------------------------------------------------------------
FROM openfoam AS hardened

LABEL org.opencontainers.image.title="cfd-agent" \
      org.opencontainers.image.description="OpenFOAM ESI v2512, Gmsh, CadQuery, PyVista and cfdagent" \
      org.opencontainers.image.source="https://github.com/mkrajew/cfd-agent" \
      org.opencontainers.image.licenses="MIT"

USER root

# Runtime libraries only (no -dev packages):
#  - libgl1, libglu1-mesa, libx*, libxft2, libfontconfig1: linked by libgmsh (Gmsh wheel) and the
#    OCP (CadQuery) wheel at import time, even when no window is ever opened
#  - libegl1, libegl-mesa0, libgl1-mesa-dri: headless OpenGL for PyVista/VTK through EGL, with
#    Mesa's software rasteriser (no GPU, no X server)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libegl-mesa0 \
        libegl1 \
        libfontconfig1 \
        libgl1 \
        libgl1-mesa-dri \
        libglu1-mesa \
        libxcursor1 \
        libxfixes3 \
        libxft2 \
        libxinerama1 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# No compilers. Case-supplied C++ (#codeStream, coded patch types) is refused below, and nothing
# in the pipeline compiles OpenFOAM code, so compilers would only help an attacker. Purging the
# development metapackages of OpenFOAM and the compilers leaves the `openfoam2512` runtime package
# (applications, libraries, etc, tutorials) and its runtime dependencies untouched. Checked by the
# integration suite and by the library check below. (`sudo` cannot be purged: its pre-removal
# script fails in a container; its setuid bit is stripped below.)
RUN apt-get purge -y gcc g++ gfortran cpp make \
        openfoam2512-default openfoam2512-dev openfoam2512-tools \
    && apt-get autoremove -y --purge \
    && rm -rf /var/lib/apt/lists/* \
    && for tool in gcc g++ cc c++ gfortran make cpp; do \
        if command -v "${tool}" >/dev/null; then echo "${tool} is still installed" >&2; exit 1; fi; \
    done

# The base image has a user `ubuntu` with uid 1000. Replace it by `cfd` (uid 1000).
RUN userdel -r ubuntu \
    && groupadd --gid 1000 cfd \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash cfd \
    && mkdir -p /work \
    && chown cfd:cfd /work

# OpenFOAM environment for `docker run <image> <cmd>` (entrypoint), for `bash -c` shells
# (BASH_ENV) and for interactive shells (the base image's /etc/bash.bashrc).
COPY --chmod=0755 docker/openfoam-env.sh /etc/cfd/openfoam-env.sh
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/cfd-entrypoint

# Case-supplied code is never allowed: allowSystemOperations 0 in the OpenFOAM project controlDict
# (OpenFOAM reads ~/.OpenFOAM/<version>/controlDict, ~/.OpenFOAM/controlDict and this file; there
# is no separate site-level file). With it off, OpenFOAM refuses #codeStream, #calc, coded patch
# types and coded function objects before anything is generated or compiled. The assertions fail
# the build when the line is missing or not 0 (for example after a base image bump), and when the
# running OpenFOAM does not report the switch as off.
ARG OPENFOAM_CONTROL_DICT=/usr/lib/openfoam/openfoam2512/etc/controlDict
RUN sed -i 's/^\([[:space:]]*allowSystemOperations[[:space:]]\{1,\}\)1[[:space:]]*;/\10;/' \
        "${OPENFOAM_CONTROL_DICT}" \
    && [ "$(grep -Ec '^[[:space:]]*allowSystemOperations([[:space:];]|$)' "${OPENFOAM_CONTROL_DICT}")" = 1 ] \
    && [ "$(grep -Ec '^[[:space:]]*allowSystemOperations[[:space:]]+0[[:space:]]*;' "${OPENFOAM_CONTROL_DICT}")" = 1 ] \
    && bash -c '. /etc/cfd/openfoam-env.sh && icoFoam -listSwitches' \
        | grep -q 'allowSystemOperations : Disallowing user-supplied system call operations'

# Every OpenFOAM application and library must still find the system libraries it links against
# after the purge above (the build fails on a missing library; nothing is run).
RUN bash -c '. /etc/cfd/openfoam-env.sh \
        && missing="$(ldd "${FOAM_APPBIN}"/* "${FOAM_LIBBIN}"/*.so 2>/dev/null | grep "not found" | sort -u)" \
        && if [ -n "${missing}" ]; then echo "${missing}" >&2; exit 1; fi'

# No setuid or setgid binaries (su, sudo, mount, passwd, ...): they are no use to the unprivileged
# user and a risk if anything ever runs with more privileges. The last step before the
# assertion, so nothing above can bring them back.
RUN find / -xdev -type f -perm /6000 -exec chmod ug-s {} + \
    && [ -z "$(find / -xdev -type f -perm /6000)" ]

ENV BASH_ENV=/etc/cfd/openfoam-env.sh \
    PATH="/opt/venv/bin:${PATH}" \
    VTK_DEFAULT_OPENGL_WINDOW=vtkEGLRenderWindow \
    PYVISTA_OFF_SCREEN=true \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    PYTHONDONTWRITEBYTECODE=1

# --------------------------------------------------------------------------------------------
# test: the development image (integration tests run here, with the repository bind-mounted).
# --------------------------------------------------------------------------------------------
FROM hardened AS test
COPY --from=venv-test /opt/uv-python /opt/uv-python
COPY --from=venv-test /opt/venv /opt/venv
ENV PYTEST_ADDOPTS="-p no:cacheprovider"
# The repository is bind-mounted here at run time; tests write only to pytest's tmp_path.
USER cfd
WORKDIR /work
ENTRYPOINT ["cfd-entrypoint"]
CMD ["bash"]

# --------------------------------------------------------------------------------------------
# runtime: the published image. Keep it the LAST stage: it is the default build target.
# --------------------------------------------------------------------------------------------
FROM hardened AS runtime
COPY --from=venv-runtime /opt/uv-python /opt/uv-python
COPY --from=venv-runtime /opt/venv /opt/venv
USER cfd
WORKDIR /work
ENTRYPOINT ["cfd-entrypoint"]
CMD ["bash"]
