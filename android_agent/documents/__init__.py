"""Documents: the agent writes a script, the script writes the file."""

from .reader import (
    ALLOWED_FORMATS,
    MAX_PDF_PAGES,
    OPTIONAL_LIBRARIES,
    ReadError,
    available_libraries,
    count_pdf_pages,
    library_summary,
    read_file_text,
)
from .sandbox import (
    ScriptResult,
    clear_workspace,
    pick_output,
    proot_available,
    run_script,
    workspace_for,
    workspaces_root,
)
from .store import DocumentStore, documents_root, new_document_id, slugify

__all__ = [
    "ALLOWED_FORMATS",
    "MAX_PDF_PAGES",
    "OPTIONAL_LIBRARIES",
    "DocumentStore",
    "ReadError",
    "ScriptResult",
    "available_libraries",
    "clear_workspace",
    "count_pdf_pages",
    "documents_root",
    "library_summary",
    "new_document_id",
    "pick_output",
    "proot_available",
    "read_file_text",
    "run_script",
    "slugify",
    "workspace_for",
    "workspaces_root",
]
