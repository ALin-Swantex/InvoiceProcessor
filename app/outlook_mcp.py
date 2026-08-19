from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.environment import load_project_environment
from app.outlook_graph import OutlookGraphClient, OutlookSettings


load_project_environment()

mcp = FastMCP(
    "Invoice Outlook",
    instructions=(
        "Read invoice email metadata and PDF attachments from the configured "
        "Microsoft 365 shared mailbox. These tools never send, delete, move, "
        "or mark email as read."
    ),
    host=os.environ.get("OUTLOOK_MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("OUTLOOK_MCP_PORT", "8001")),
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
)


@lru_cache(maxsize=1)
def get_graph_client() -> OutlookGraphClient:
    settings = OutlookSettings(
        tenant_id=os.environ.get("OUTLOOK_MCP_TENANT_ID", ""),
        client_id=os.environ.get("OUTLOOK_MCP_CLIENT_ID", ""),
        client_secret=os.environ.get("OUTLOOK_MCP_CLIENT_SECRET", ""),
        mailbox=os.environ.get("OUTLOOK_MCP_MAILBOX", ""),
        download_directory=Path(
            os.environ.get("OUTLOOK_MCP_DOWNLOAD_DIR", "outlook_downloads")
        ),
        max_pdf_bytes=int(
            os.environ.get("OUTLOOK_MCP_MAX_PDF_BYTES", str(20 * 1024 * 1024))
        ),
    )
    return OutlookGraphClient(settings)


@mcp.tool()
def list_invoice_emails(
    limit: int = 20, unread_only: bool = True
) -> list[dict[str, Any]]:
    """List recent mailbox messages that contain attachments."""
    return get_graph_client().list_invoice_emails(
        limit=limit, unread_only=unread_only
    )


@mcp.tool()
def get_invoice_email(message_id: str) -> dict[str, Any]:
    """Get safe metadata for one mailbox message by its Graph message ID."""
    return get_graph_client().get_invoice_email(message_id)


@mcp.tool()
def list_pdf_attachments(message_id: str) -> list[dict[str, Any]]:
    """List non-inline PDF attachments for a mailbox message."""
    return get_graph_client().list_pdf_attachments(message_id)


@mcp.tool()
def download_pdf_attachment(
    message_id: str, attachment_id: str, filename: str
) -> dict[str, str]:
    """Download one PDF into the configured local invoice staging directory."""
    path = get_graph_client().download_pdf_attachment(
        message_id, attachment_id, filename
    )
    return {"stored_path": str(path)}


def main() -> None:
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
