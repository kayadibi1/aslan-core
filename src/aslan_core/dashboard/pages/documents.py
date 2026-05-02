"""Documents page — ``/documents``."""

from __future__ import annotations

from fasthtml.common import H1, Div, Table, Tbody, Td, Th, Thead, Tr
from starlette.requests import Request
from starlette.responses import HTMLResponse

from aslan_core.dashboard import queries
from aslan_core.dashboard.app import app, get_session_factory
from aslan_core.dashboard.formatters import format_id_prefix, format_size_bytes
from aslan_core.dashboard.render import register_template, render
from aslan_core.dashboard.view_models import DocumentRowVM, DocumentsVM


def _build_documents_body(vm: DocumentsVM) -> object:
    headers = (
        "filing_id",
        "source",
        "entity",
        "kind",
        "published_at",
        "ingested_at",
        "attachments",
        "body_size",
        "redacted_at",
    )
    return Div(
        H1("Documents"),
        Table(
            Thead(Tr(*[Th(h) for h in headers])),
            Tbody(*[_document_row(r) for r in vm.rows]),
            cls="aslan-table",
        ),
        cls="aslan-documents",
    )


def _document_row(row: DocumentRowVM) -> object:
    return Tr(
        Td(format_id_prefix(str(row.filing_id))),
        Td(row.source_id),
        Td(row.entity_name or "—"),
        Td(row.filing_kind),
        Td(row.published_at.isoformat()),
        Td(row.ingested_at.isoformat()),
        Td(str(row.attachment_count)),
        Td(format_size_bytes(row.body_size_bytes) if row.body_size_bytes is not None else "—"),
        Td(row.redacted_at.isoformat() if row.redacted_at else "—"),
    )


register_template(DocumentsVM, _build_documents_body)


@app.get("/documents")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def documents_page(request: Request) -> HTMLResponse:
    factory = get_session_factory()
    async with factory() as session:
        vm = await queries.documents_recent(session, limit=50)
    return render(request, vm)
