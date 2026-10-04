"""Extract readable text from files, including formats the agent did not write.

Separate from script execution on purpose: reading a PDF someone sent is a
very different operation from running code, and only one of them needs a
sandbox.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Extensions a generated document may have. The script decides everything
#: about the contents; this only bounds what it is allowed to hand back.
ALLOWED_FORMATS = (
    "pdf", "xlsx", "csv", "json", "html", "md", "txt", "pptx", "png", "docx",
)

#: Libraries worth telling the model about, with the command that installs
#: each. Reported at call time rather than assumed, because a phone is not a
#: build server and any of these may be missing.
OPTIONAL_LIBRARIES = {
    "reportlab": "pip install reportlab",
    "openpyxl": "pip install openpyxl",
    "matplotlib": "pip install matplotlib",
    "pypdf": "pip install pypdf",
    "pptx": "pkg install python-lxml python-pillow && pip install python-pptx",
    "docx": "pip install python-docx",
    "PIL": "pkg install python-pillow",
}


class ReadError(RuntimeError):
    def __init__(self, message: str, *, code: str = "read_failed"):
        super().__init__(message)
        self.code = code


def available_libraries() -> dict[str, bool]:
    """Which optional libraries a generated script could import."""
    found = {}
    for name in OPTIONAL_LIBRARIES:
        try:
            __import__(name)
            found[name] = True
        except Exception:
            found[name] = False
    return found


def library_summary() -> str:
    """One line listing what a script may use, for the tool description."""
    found = available_libraries()
    usable = sorted(name for name, ok in found.items() if ok)
    return ", ".join(usable) if usable else "the standard library only"


def read_file_text(path: Path, *, limit: int = 20000) -> str:
    """Extract readable text from a file the agent may not have written."""
    suffix = path.suffix.lower().lstrip(".")
    if suffix == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ReadError(
                "Reading PDF needs pypdf, which is not installed: pip install pypdf",
                code="backend_missing",
            ) from exc
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages)[:limit]
    if suffix == "xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise ReadError(
                "Reading XLSX needs openpyxl: pip install openpyxl",
                code="backend_missing",
            ) from exc
        workbook = load_workbook(str(path), read_only=True, data_only=True)
        lines = []
        for sheet in workbook.worksheets:
            lines.append(f"# {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                lines.append(
                    ", ".join("" if cell is None else str(cell) for cell in row)
                )
        return "\n".join(lines)[:limit]
    if suffix == "pptx":
        try:
            from pptx import Presentation
        except ImportError as exc:
            raise ReadError(
                "Reading PPTX needs python-pptx: pip install python-pptx",
                code="backend_missing",
            ) from exc
        presentation = Presentation(str(path))
        lines = []
        for number, slide in enumerate(presentation.slides, start=1):
            lines.append(f"## Slide {number}")
            for shape in slide.shapes:
                if shape.has_text_frame and shape.text_frame.text.strip():
                    lines.append(shape.text_frame.text)
        return "\n".join(lines)[:limit]
    # Everything else is text, read defensively.
    return path.read_text(encoding="utf-8", errors="replace")[:limit]
