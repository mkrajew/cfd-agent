# syntax=docker/dockerfile:1
#
# cfdagent:dev - OpenFOAM ESI + Gmsh + CadQuery + PyVista + the cfdagent package.
#
# Build (from the repository root):  docker build -f docker/Dockerfile -t cfdagent:dev .
# Integration tests (repo bind-mounted at /work): see "Key commands" in CLAUDE.md.

# uv, pinned by tag and digest. A stage of its own so Dependabot can update it.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

# OpenFOAM ESI v2512 (openfoam.com), the latest release with an official image on Docker Hub
# (opencfd/openfoam-default). Ubuntu 24.04, linux/amd64 + linux/arm64. Pinned by tag and digest.
FROM opencfd/openfoam-default:2512@sha256:33fb575aa9980d2bc42fd58c75ae698c489293ba30c991380fe3f899c622f319

LABEL org.opencontainers.image.title="cfd-agent" \
      org.opencontainers.image.description="OpenFOAM ESI v2512, Gmsh, CadQuery, PyVista and cfdagent" \
      org.opencontainers.image.source="https://github.com/mkrajew/cfd-agent" \
      org.opencontainers.image.licenses="MIT"

USER root

# Python 3.12 (uv-managed; the base image ships no Python) and the locked environment.
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_PYTHON=3.12 \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1

WORKDIR /opt/build
# Layer 1: third-party dependencies from uv.lock (changes rarely).
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --extra toolchain --group dev --no-install-project
# Layer 2: the cfdagent package itself, installed (non-editable) from the build context.
COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --extra toolchain --group dev --no-editable \
    && cd / && rm -rf /opt/build

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

ENV PATH="/opt/venv/bin:${PATH}" \
    BASH_ENV=/etc/cfd/openfoam-env.sh \
    VTK_DEFAULT_OPENGL_WINDOW=vtkEGLRenderWindow \
    PYVISTA_OFF_SCREEN=true \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTEST_ADDOPTS="-p no:cacheprovider"

# The repository is bind-mounted here at run time; tests write only to pytest's tmp_path.
USER cfd
WORKDIR /work
ENTRYPOINT ["cfd-entrypoint"]
CMD ["bash"]
