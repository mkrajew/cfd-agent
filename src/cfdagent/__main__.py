"""``python -m cfdagent``: the same command line as the ``cfdagent`` script."""

from cfdagent.cli import app

if __name__ == "__main__":
    app(prog_name="cfdagent")
