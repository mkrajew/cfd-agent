"""Host side of the execution sandbox: build the ``docker run`` command, call executor ops.

Nothing here imports the CAD, mesh or OpenFOAM tool layer; the tools run inside the container.
"""
