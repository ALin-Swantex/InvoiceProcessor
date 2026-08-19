from __future__ import annotations

from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult


class OutlookMcpClientError(RuntimeError):
    pass


class OutlookMcpClient:
    def __init__(self, url: str) -> None:
        self.url = url

    async def list_invoice_emails(
        self, limit: int = 20, unread_only: bool = True
    ) -> list[dict[str, Any]]:
        result = await self._call(
            "list_invoice_emails",
            {"limit": limit, "unread_only": unread_only},
        )
        if not isinstance(result, list) or not all(
            isinstance(item, dict) for item in result
        ):
            raise OutlookMcpClientError("MCP returned an invalid email list.")
        return result

    async def get_invoice_email(self, message_id: str) -> dict[str, Any]:
        result = await self._call("get_invoice_email", {"message_id": message_id})
        if not isinstance(result, dict):
            raise OutlookMcpClientError("MCP returned invalid email metadata.")
        return result

    async def list_pdf_attachments(
        self, message_id: str
    ) -> list[dict[str, Any]]:
        result = await self._call(
            "list_pdf_attachments", {"message_id": message_id}
        )
        if not isinstance(result, list) or not all(
            isinstance(item, dict) for item in result
        ):
            raise OutlookMcpClientError("MCP returned an invalid attachment list.")
        return result

    async def download_pdf_attachment(
        self, message_id: str, attachment_id: str, filename: str
    ) -> str:
        result = await self._call(
            "download_pdf_attachment",
            {
                "message_id": message_id,
                "attachment_id": attachment_id,
                "filename": filename,
            },
        )
        if not isinstance(result, dict) or not isinstance(
            result.get("stored_path"), str
        ):
            raise OutlookMcpClientError("MCP returned an invalid PDF path.")
        return result["stored_path"]

    async def _call(self, name: str, arguments: dict[str, Any]) -> Any:
        async with streamable_http_client(self.url) as (
            read_stream,
            write_stream,
            _,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
        return self._extract_result(result)

    @staticmethod
    def _extract_result(result: CallToolResult) -> Any:
        if result.isError:
            text = "Unknown MCP tool error."
            if result.content and hasattr(result.content[0], "text"):
                text = str(result.content[0].text)
            raise OutlookMcpClientError(text)

        structured = result.structuredContent
        if isinstance(structured, dict):
            if "result" in structured:
                return structured["result"]
            return structured

        if result.content and hasattr(result.content[0], "text"):
            import json

            try:
                return json.loads(str(result.content[0].text))
            except json.JSONDecodeError as error:
                raise OutlookMcpClientError(
                    "MCP returned non-JSON tool content."
                ) from error
        raise OutlookMcpClientError("MCP tool returned no content.")
