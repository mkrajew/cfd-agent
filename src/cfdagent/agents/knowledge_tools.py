"""The knowledge tools (``search_knowledge``, ``read_knowledge``) of the stages that offer them.

A stage mixes in ``KnowledgeTools`` and adds ``self.knowledge_tools(ctx)`` to its extra tools. The
tools call the executor ops of the same names: they reach the curated notes of the package
(``note:<file>``) and the OpenFOAM tutorials of the image, and nothing else (never ``benchmarks/``).

``knowledge_reads`` lists every path the agent asked ``read_knowledge`` for, in order of first
request, whether or not the read succeeded. The orchestrator stores it in the run record (a
blind-evaluation audit: what did the agent look at?).

This module is host-side: it imports no tool, CAD, mesh or knowledge code.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from cfdagent.agents.stage import StageContext
from cfdagent.agents.tools import Tool
from cfdagent.errors import AgentInputError, ToolFailure
from cfdagent.executor.protocol import ReadKnowledgeArgs, SearchKnowledgeArgs
from cfdagent.sandbox.client import outer_timeout

KNOWLEDGE_TIMEOUT_S = 60.0
READ_BYTES = 7000  # a tool result is cut at 8000 bytes; this keeps the truncation marker visible


class SearchKnowledgeToolArgs(BaseModel):
    query: str = Field(
        max_length=200,
        description="Words to look for (all of them must occur, in the file's path or its text)",
    )


class ReadKnowledgeToolArgs(BaseModel):
    path: str = Field(
        min_length=1,
        max_length=1024,
        description="A path as search_knowledge shows it: note:<file>.md for a curated note, else "
        "relative to the tutorial directory; a directory path lists its entries",
    )


class KnowledgeTools:
    """Mixin: the two knowledge tools and the audit list of what the agent read."""

    def __init__(self) -> None:
        super().__init__()
        self.knowledge_reads: list[str] = []

    def knowledge_tools(self, ctx: StageContext) -> list[Tool]:
        return [
            Tool(
                "search_knowledge",
                "Search the curated notes and the OpenFOAM tutorials of this installation. A "
                "file matches when all the words occur in its path (case and file names) or its "
                "text, in any case. Returns up to 10 lines `path:line: text`; the notes, shown as "
                "`note:<file>`, are curated rules and come first, so read them before the "
                "tutorials.",
                SearchKnowledgeToolArgs,
                lambda args: self._knowledge_search(ctx, args),
            ),
            Tool(
                "read_knowledge",
                "Read one note (`note:<file>`) or one file of the OpenFOAM tutorials (a path from "
                f"search_knowledge), or list a tutorial directory. At most {READ_BYTES} bytes are "
                "shown.",
                ReadKnowledgeToolArgs,
                lambda args: self._knowledge_read(ctx, args),
            ),
        ]

    @staticmethod
    def _knowledge_call(ctx: StageContext, op: str, args: BaseModel) -> dict:
        response = ctx.sandbox.call(op, args, outer_timeout(ctx.inner_timeout(KNOWLEDGE_TIMEOUT_S)))
        if response.internal:
            raise ToolFailure(f"{op} failed outside the agent's work: {response.error}")
        if not response.ok:
            raise AgentInputError(response.error or f"{op} failed without a message")
        return response.data or {}

    def _knowledge_search(self, ctx: StageContext, args: SearchKnowledgeToolArgs) -> str:
        data = self._knowledge_call(ctx, "search_knowledge", SearchKnowledgeArgs(query=args.query))
        hits = data.get("hits")
        if not isinstance(hits, list):
            raise ToolFailure("search_knowledge answered without a list of hits")
        if not hits:
            return (
                "No note or tutorial file contains all of these words. Try fewer or different "
                "words, for example a solver name, a dictionary name or a keyword."
            )
        lines = []
        for hit in hits:
            path, line, text = str(hit["path"]), int(hit["line"]), str(hit["text"])
            lines.append(f"{path}:{line}: {text}" if line else f"{path} {text}")
        return "\n".join(lines)

    def _knowledge_read(self, ctx: StageContext, args: ReadKnowledgeToolArgs) -> str:
        if args.path not in self.knowledge_reads:
            self.knowledge_reads.append(args.path)
        request = ReadKnowledgeArgs(path=args.path, max_bytes=READ_BYTES)
        data = self._knowledge_call(ctx, "read_knowledge", request)
        text = data.get("text")
        if not isinstance(text, str):
            raise ToolFailure("read_knowledge answered without text")
        return text
