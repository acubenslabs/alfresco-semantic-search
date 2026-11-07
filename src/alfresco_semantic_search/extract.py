"""Text extraction through Alfresco's own transform engine.

The obvious approach is to download the original and extract text locally with
``pdfplumber``, ``python-docx`` and friends. This module does something better:
it hands the bytes to ``alfresco-transform-core-aio``, the same service the
repository uses to extract text for its search index.

Two reasons that matters:

* **Consistency.** The text you embed is byte-for-byte the text Alfresco
  indexed. Semantic and keyword search see the same document.
* **Coverage.** Every format the repository can handle — Office, OpenDocument,
  PDF, email, and images when OCR is configured — works with no per-format
  library to maintain on your side.

The cost is one HTTP round trip and a dependency on the transform service
being reachable, which in an Alfresco deployment it already is.
"""

from __future__ import annotations

import logging
import mimetypes
import urllib.error
import urllib.request
import uuid

log = logging.getLogger(__name__)

#: Formats the transform service will not produce useful text for.
_SKIP_MIME_PREFIXES = ("video/", "audio/")


class ExtractionError(RuntimeError):
    """The transform service could not produce text for this document."""


class TransformClient:
    """Client for ``alfresco-transform-core-aio``.

    .. note::
       Use ``/ready`` and ``/live`` for health checks, not
       ``/actuator/health``. The aggregate actuator endpoint reports ``DOWN``
       with HTTP 503 on a perfectly healthy service, while its own
       ``liveness`` and ``readiness`` groups both report ``UP`` — a false
       negative that will page you at 3am for nothing.
    """

    def __init__(self, base_url: str = "http://localhost:8090", timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def ready(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.base_url}/ready")
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def to_text(self, content: bytes, source_mime: str, filename: str = "document") -> str:
        """Extract plain text from ``content``.

        Raises :class:`ExtractionError` when the service declines the
        conversion, which is normal for formats that hold no text.
        """
        if source_mime.startswith(_SKIP_MIME_PREFIXES):
            raise ExtractionError(f"no text to extract from {source_mime}")

        body, content_type = _multipart(
            fields={"sourceMimetype": source_mime, "targetMimetype": "text/plain"},
            file_field="file",
            filename=filename,
            file_bytes=content,
            file_mime=source_mime,
        )
        req = urllib.request.Request(
            f"{self.base_url}/transform",
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise ExtractionError(f"transform returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ExtractionError(f"transform service unreachable: {exc.reason}") from exc


def guess_mime(filename: str, fallback: str = "application/octet-stream") -> str:
    """Best-effort MIME type from a filename."""
    return mimetypes.guess_type(filename)[0] or fallback


def _multipart(
    fields: dict[str, str],
    file_field: str,
    filename: str,
    file_bytes: bytes,
    file_mime: str,
) -> tuple[bytes, str]:
    """Build a ``multipart/form-data`` body without pulling in ``requests``."""
    boundary = f"----alfrescoSemanticSearch{uuid.uuid4().hex}"
    parts: list[bytes] = []

    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )

    safe_name = filename.replace('"', "_")
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}"; filename="{safe_name}"\r\n'
        f"Content-Type: {file_mime}\r\n\r\n".encode()
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())

    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
