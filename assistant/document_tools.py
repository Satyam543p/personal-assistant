"""
Document Ingestion & Capability Tools for Kate Personal Assistant.
Implements:
  - Real PDF text extraction with pypdf
  - Multi-page chunking
  - Honest OCR capability gap reporting for scanned/image PDFs
  - Zero Fake Email Tools rule compliance

Following Workspace Design Rules: Abstractions & Interfaces First.
"""

import abc
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from assistant.tools import ErrorCode, Tool, ToolResult, is_path_allowed
except ModuleNotFoundError:
    from tools import ErrorCode, Tool, ToolResult, is_path_allowed

logger = logging.getLogger("kate.document_tools")


# =====================================================================
# Document Reader Abstraction
# =====================================================================

class AbstractDocumentReader(abc.ABC):
    """Abstract interface for extracting text and structure from local documents."""

    @abc.abstractmethod
    def read_document(self, file_path: str, max_pages: Optional[int] = None) -> Dict[str, Any]:
        """Reads document content, returning metadata, text, and chunked sections."""
        pass


class PDFDocumentReader(AbstractDocumentReader):
    """Concrete PDF document reader leveraging pypdf."""

    def __init__(self, chunk_size: int = 2000):
        self.chunk_size = chunk_size

    def read_document(self, file_path: str, max_pages: Optional[int] = None) -> Dict[str, Any]:
        path_obj = Path(file_path).resolve(strict=False)
        if not path_obj.exists():
            return {
                "ok": False,
                "error_code": ErrorCode.FILE_NOT_FOUND,
                "message": f"Document file not found: '{file_path}'"
            }

        try:
            from pypdf import PdfReader
        except ImportError:
            return {
                "ok": False,
                "error_code": ErrorCode.CAPABILITY_GAP,
                "message": "pypdf library is not installed. PDF ingestion is unavailable."
            }

        try:
            reader = PdfReader(str(path_obj))
            num_pages = len(reader.pages)
            limit = min(num_pages, max_pages) if max_pages else num_pages

            extracted_text = []
            for i in range(limit):
                page = reader.pages[i]
                page_text = page.extract_text() or ""
                extracted_text.append(page_text.strip())

            full_text = "\n\n".join([t for t in extracted_text if t])

            # Honest OCR gap detection: if file has pages but virtually zero text
            if num_pages > 0 and len(full_text.strip()) < 20:
                return {
                    "ok": False,
                    "is_scanned": True,
                    "error_code": ErrorCode.CAPABILITY_GAP,
                    "message": (
                        "PDF appears to be scanned or image-only without an embedded text layer. "
                        "OCR capability is currently not installed."
                    )
                }

            # Chunk into manageable sections
            chunks: List[str] = []
            for i in range(0, len(full_text), self.chunk_size):
                chunks.append(full_text[i:i + self.chunk_size])

            return {
                "ok": True,
                "is_scanned": False,
                "total_pages": num_pages,
                "pages_read": limit,
                "char_count": len(full_text),
                "text": full_text,
                "chunks": chunks
            }
        except Exception as e:
            logger.error(f"PDF read error for '{file_path}': {e}")
            return {
                "ok": False,
                "error_code": ErrorCode.EXECUTION_FAILED,
                "message": f"Failed reading PDF: {e}"
            }


# =====================================================================
# Document Tools
# =====================================================================

class ReadPdfTool(Tool):
    """Tool to inspect and extract text from local PDF documents."""

    def __init__(self, reader: Optional[AbstractDocumentReader] = None):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute or relative path to PDF file"},
                    "max_pages": {"type": "integer", "description": "Optional limit on pages to read", "default": 20}
                },
                "required": ["file_path"]
            },
            "side_effects": "none",
            "timeout_ms": 10000,
            "memory_limit_mb": 100
        }
        super().__init__("read_pdf", "read_only", declaration)
        self.reader = reader or PDFDocumentReader()

    async def execute(self, executor=None, **kwargs) -> dict:
        raw_path = kwargs.get("file_path") or kwargs.get("path")
        if not raw_path:
            return ToolResult(
                ok=False,
                error_code=ErrorCode.EXECUTION_FAILED,
                message="Missing required parameter 'file_path'.",
                status="failure"
            ).to_dict()

        if not is_path_allowed(raw_path):
            return ToolResult(
                ok=False,
                error_code=ErrorCode.PATH_FORBIDDEN,
                message=f"Access to path '{raw_path}' is forbidden.",
                status="failure"
            ).to_dict()

        max_pages = kwargs.get("max_pages")
        res = self.reader.read_document(raw_path, max_pages=max_pages)
        if not res.get("ok"):
            return ToolResult(
                ok=False,
                error_code=res.get("error_code", ErrorCode.EXECUTION_FAILED),
                message=res.get("message", "Failed to extract PDF text."),
                status="failure"
            ).to_dict()

        total = res.get("total_pages", 0)
        char_count = res.get("char_count", 0)
        preview = res.get("text", "")[:300]

        return ToolResult(
            ok=True,
            data=res,
            message=f"Successfully extracted {char_count} characters across {total} page(s).\nPreview:\n{preview}...",
            status="success"
        ).to_dict()


class SendEmailTool(Tool):
    """
    Honest Email Dispatch Placeholder Tool.
    Enforces the Zero Fake Tools Rule by declaring the integration boundary honestly.
    """

    def __init__(self):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "attachment": {"type": "string"}
                },
                "required": ["to", "subject"]
            },
            "side_effects": "none",
            "timeout_ms": 3000,
            "memory_limit_mb": 50
        }
        super().__init__("send_email", "read_only", declaration)

    async def execute(self, executor=None, **kwargs) -> dict:
        recipient = kwargs.get("to")
        subject = kwargs.get("subject", "Kate Report")
        attachment = kwargs.get("attachment")

        attach_msg = f" with attachment '{attachment}'" if attachment else ""
        return ToolResult(
            ok=False,
            error_code=ErrorCode.CAPABILITY_GAP,
            message=(
                f"Direct SMTP/Email dispatch to '{recipient}' for '{subject}'{attach_msg} is not configured. "
                "I can save the report to your Desktop and open your system default mail client, "
                "or assist you in creating a custom email skill."
            ),
            status="failure"
        ).to_dict()
