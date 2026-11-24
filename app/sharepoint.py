from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import PurePosixPath
from threading import Lock
from time import monotonic
from typing import Any, Callable
from urllib.parse import quote

import httpx

from app.graph_http import GRAPH_BASE_URL, GraphSession
from app.outlook_graph import OutlookSettings, default_token_provider
from app.company_folders import FLAGGED_INVOICES_FOLDER



class SharePointConfigurationError(RuntimeError):
    pass


class SharePointError(RuntimeError):
    pass


@dataclass(frozen=True)
class SharePointSettings:
    site_id: str
    drive_id: str
    incoming_folder: str
    flagged_folder: str = FLAGGED_INVOICES_FOLDER

    def validate(self) -> None:
        values = {
            "SHAREPOINT_SITE_ID": self.site_id,
            "SHAREPOINT_DRIVE_ID": self.drive_id,
            "SHAREPOINT_INCOMING_FOLDER": self.incoming_folder,
            "SHAREPOINT_FLAGGED_FOLDER": self.flagged_folder,
        }
        missing = [name for name, value in values.items() if not value.strip()]
        if missing:
            raise SharePointConfigurationError(
                f"Missing required SharePoint settings: {', '.join(missing)}."
            )


class SharePointClient:
    """Uploads and moves invoice PDFs within a SharePoint document library
    using the Microsoft Graph Drive API.

    All operations target a single configured drive (document library).
    Folder paths are relative to the drive root.
    """

    def __init__(
        self,
        sharepoint_settings: SharePointSettings,
        outlook_settings: OutlookSettings,
        *,
        token_provider: Callable[[], str] | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        sharepoint_settings.validate()
        self.settings = sharepoint_settings
        self.token_provider = token_provider or default_token_provider(outlook_settings)
        self.http_client = http_client or httpx.Client(timeout=60.0)
        self.session = GraphSession(
            self.http_client,
            self.token_provider,
            error_class=SharePointError,
        )
        self._cache_lock = Lock()
        self._folder_cache: dict[str, tuple[float, str]] = {}
        self._folder_inventory_cache: dict[int, tuple[float, list[str]]] = {}
        self._statement_library_cache: dict[
            tuple[str, int],
            tuple[float, dict[str, list[dict[str, Any]]]],
        ] = {}
        self._cache_ttl_seconds = 300.0

    def upload_to_incoming(
        self,
        filename: str,
        content: bytes,
        *,
        conflict_behavior: str = "rename",
    ) -> dict[str, Any]:
        """Upload a PDF to the configured incoming invoices folder.

        Returns the Graph DriveItem for the uploaded file.
        """
        return self._upload(
            self.settings.incoming_folder,
            filename,
            content,
            conflict_behavior=conflict_behavior,
        )

    def list_incoming_pdfs(self) -> list[dict[str, Any]]:
        """Return PDFs currently waiting in the configured Incoming folder."""
        drive_id = quote(self.settings.drive_id, safe="")
        folder = quote(str(PurePosixPath(self.settings.incoming_folder)), safe="/")
        url = f"{GRAPH_BASE_URL}/drives/{drive_id}/root:/{folder}:/children"
        params: dict[str, str] = {
            "$select": "id,name,size,file,webUrl,createdDateTime",
            "$top": "200",
        }
        items: list[dict[str, Any]] = []
        for payload in self.session.paginate(
            url, params=params, operation="SharePoint Incoming listing"
        ):
            page = payload.get("value", [])
            if not isinstance(page, list):
                raise SharePointError(
                    "SharePoint returned an invalid Incoming folder response."
                )
            items.extend(
                item
                for item in page
                if isinstance(item, dict)
                and isinstance(item.get("file"), dict)
                and str(item.get("name", "")).lower().endswith(".pdf")
            )
        return items

    def download_item(self, item_id: str) -> bytes:
        """Download a DriveItem by ID for processing or preview caching."""
        url = (
            f"{GRAPH_BASE_URL}/drives/{quote(self.settings.drive_id, safe='')}/"
            f"items/{quote(item_id, safe='')}/content"
        )
        return self.session.request(
            "GET",
            url,
            operation="SharePoint invoice download",
            follow_redirects=True,
        ).content

    def delete_item(self, item_id: str) -> None:
        """Permanently delete a DriveItem from the configured library."""
        url = (
            f"{GRAPH_BASE_URL}/drives/{quote(self.settings.drive_id, safe='')}/"
            f"items/{quote(item_id, safe='')}"
        )
        self.session.request(
            "DELETE",
            url,
            operation="SharePoint invoice deletion",
            ok_statuses={404},
        )

    def move_to_folder(
        self,
        source_item_id: str,
        destination_folder: str,
        destination_filename: str,
    ) -> dict[str, Any]:
        """Move a DriveItem to a destination folder, optionally renaming it.

        destination_folder is relative to the drive root, e.g.
        "Invoices/Company A/PO Matching".

        Returns the updated Graph DriveItem.
        """
        folder_id = self._ensure_folder(destination_folder)
        url = (
            f"{GRAPH_BASE_URL}/drives/{quote(self.settings.drive_id, safe='')}/"
            f"items/{quote(source_item_id, safe='')}"
        )
        payload: dict[str, Any] = {
            "parentReference": {"id": folder_id},
            "name": destination_filename,
        }
        # Setting the same parent and name twice is harmless, so retry it.
        response = self._send_json("PATCH", url, payload, idempotent=True)
        with self._cache_lock:
            self._statement_library_cache.clear()
        return response.json()

    def upload_and_move(
        self,
        *,
        filename: str,
        content: bytes,
        destination_folder: str,
        destination_filename: str,
    ) -> dict[str, Any]:
        """Upload a PDF to incoming, then immediately move it to the
        destination folder with the correct filename.

        This is the main entry point called after routing confirmation.
        Returns the final Graph DriveItem (post-move).
        """
        item = self.upload_to_incoming(filename, content)
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise SharePointError(
                "SharePoint did not return a valid item ID after upload."
            )
        return self.move_to_folder(item_id, destination_folder, destination_filename)

    def get_item_web_url(self, item: dict[str, Any]) -> str | None:
        return item.get("webUrl") if isinstance(item.get("webUrl"), str) else None

    def list_folder_paths(self, *, max_folders: int = 2000) -> list[str]:
        """Return every existing folder path in the configured drive.

        Paths are relative to the document-library root and are suitable for
        storing in company routing configuration.
        """
        if max_folders < 1:
            raise ValueError("max_folders must be greater than zero.")
        with self._cache_lock:
            cached = self._folder_inventory_cache.get(max_folders)
            if cached is not None and monotonic() - cached[0] < self._cache_ttl_seconds:
                return list(cached[1])

        drive_prefix = (
            f"{GRAPH_BASE_URL}/drives/{quote(self.settings.drive_id, safe='')}"
        )
        paths: set[str] = set()
        for payload in self.session.paginate(
            f"{drive_prefix}/root/delta",
            params={"$top": "200"},
            operation="SharePoint folder inventory",
        ):
            page = payload.get("value", [])
            if not isinstance(page, list):
                raise SharePointError(
                    "SharePoint returned an invalid folder inventory response."
                )
            for item in page:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("folder"), dict)
                    or "deleted" in item
                ):
                    continue
                name = item.get("name")
                parent = item.get("parentReference")
                if not isinstance(name, str) or not isinstance(parent, dict):
                    continue
                parent_path = parent.get("path")
                if not isinstance(parent_path, str) or "/root:" not in parent_path:
                    continue
                relative_parent = parent_path.partition("/root:")[2].strip("/")
                path = str(PurePosixPath(relative_parent) / name)
                paths.add(path)
                if len(paths) > max_folders:
                    raise SharePointError(
                        f"SharePoint folder scan exceeded {max_folders} folders."
                    )
        result = sorted(paths, key=str.casefold)
        with self._cache_lock:
            self._folder_inventory_cache[max_folders] = (monotonic(), result)
        return list(result)

    def list_statement_library(
        self,
        *,
        statements_root: str = "Statements",
        max_items: int = 5000,
    ) -> dict[str, list[dict[str, Any]]]:
        """Return statement PDFs grouped by their direct company folder."""
        if max_items < 1:
            raise ValueError("max_items must be greater than zero.")
        normalized_root = str(PurePosixPath(statements_root)).strip("/")
        if not normalized_root:
            raise ValueError("statements_root must not be empty.")
        cache_key = (normalized_root.casefold(), max_items)
        with self._cache_lock:
            cached = self._statement_library_cache.get(cache_key)
            if cached is not None and monotonic() - cached[0] < self._cache_ttl_seconds:
                return self._copy_statement_library(cached[1])

        drive_prefix = (
            f"{GRAPH_BASE_URL}/drives/{quote(self.settings.drive_id, safe='')}"
        )
        library: dict[str, list[dict[str, Any]]] = {}
        item_count = 0
        params = {
            "$select": "id,name,size,file,folder,webUrl,createdDateTime,lastModifiedDateTime,parentReference,deleted",
            "$top": "200",
        }
        for payload in self.session.paginate(
            f"{drive_prefix}/root/delta",
            params=params,
            operation="SharePoint statement library listing",
        ):
            page = payload.get("value", [])
            if not isinstance(page, list):
                raise SharePointError(
                    "SharePoint returned an invalid statement library response."
                )
            item_count += len(page)
            if item_count > max_items:
                raise SharePointError(
                    f"SharePoint statement scan exceeded {max_items} items."
                )
            for item in page:
                if not isinstance(item, dict) or "deleted" in item:
                    continue
                name = item.get("name")
                parent = item.get("parentReference")
                if not isinstance(name, str) or not isinstance(parent, dict):
                    continue
                parent_path = parent.get("path")
                if not isinstance(parent_path, str) or "/root:" not in parent_path:
                    continue
                relative_parent = parent_path.partition("/root:")[2].strip("/")
                item_path = str(PurePosixPath(relative_parent) / name)
                parts = PurePosixPath(item_path).parts
                if len(parts) == 2 and parts[0].casefold() == normalized_root.casefold():
                    if isinstance(item.get("folder"), dict):
                        library.setdefault(parts[1], [])
                    continue
                if (
                    len(parts) == 3
                    and parts[0].casefold() == normalized_root.casefold()
                    and isinstance(item.get("file"), dict)
                    and isinstance(item.get("id"), str)
                    and name.lower().endswith(".pdf")
                ):
                    library.setdefault(parts[1], []).append(
                        {
                            "id": item.get("id"),
                            "name": name,
                            "size": item.get("size"),
                            "webUrl": item.get("webUrl"),
                            "createdDateTime": item.get("createdDateTime"),
                            "lastModifiedDateTime": item.get("lastModifiedDateTime"),
                        }
                    )

        result = {
            company: sorted(
                files,
                key=lambda item: str(item.get("name", "")).casefold(),
            )
            for company, files in sorted(library.items(), key=lambda pair: pair[0].casefold())
        }
        with self._cache_lock:
            self._statement_library_cache[cache_key] = (monotonic(), result)
        return self._copy_statement_library(result)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _upload(
        self,
        folder_path: str,
        filename: str,
        content: bytes,
        *,
        conflict_behavior: str = "rename",
    ) -> dict[str, Any]:
        if conflict_behavior not in {"fail", "replace", "rename"}:
            raise ValueError("Unsupported SharePoint upload conflict behavior.")
        safe_folder = str(PurePosixPath(folder_path))
        safe_filename = quote(filename, safe="")
        url = (
            f"{GRAPH_BASE_URL}/drives/{quote(self.settings.drive_id, safe='')}/"
            f"root:/{quote(safe_folder, safe='/')}/"
            f"{safe_filename}:/content"
        )
        # A PUT to a fixed path that replaces (or fails on conflict) is safe to
        # retry. With "rename", a retry after a lost success would create a
        # second "name 1.pdf" copy, so only throttling/connect errors retry.
        response = self.session.request(
            "PUT",
            url,
            operation="SharePoint upload",
            params={"@microsoft.graph.conflictBehavior": conflict_behavior},
            content=content,
            headers={"Content-Type": "application/pdf"},
            idempotent=conflict_behavior in {"replace", "fail"},
        )
        return response.json()

    def _ensure_folder(self, folder_path: str) -> str:
        """Return the DriveItem ID for folder_path, creating intermediate
        folders if they do not already exist."""
        parts = [p for p in PurePosixPath(folder_path).parts if p != "/"]
        if not parts:
            raise SharePointError("Destination folder path must not be empty.")

        current_id: str | None = None
        resolved_parts: list[str] = []
        for part in parts:
            resolved_parts.append(part)
            cache_key = "/".join(resolved_parts).casefold()
            cached_id = self._cached_folder_id(cache_key)
            if cached_id is not None:
                current_id = cached_id
                continue
            current_id = self._get_or_create_folder(part, parent_id=current_id)
            with self._cache_lock:
                self._folder_cache[cache_key] = (monotonic(), current_id)
        assert isinstance(current_id, str)
        return current_id

    def _cached_folder_id(self, cache_key: str) -> str | None:
        with self._cache_lock:
            cached = self._folder_cache.get(cache_key)
            if cached is None:
                return None
            if monotonic() - cached[0] >= self._cache_ttl_seconds:
                del self._folder_cache[cache_key]
                return None
            return cached[1]

    @staticmethod
    def _copy_statement_library(
        library: dict[str, list[dict[str, Any]]],
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            company: [dict(statement) for statement in statements]
            for company, statements in library.items()
        }

    def _get_or_create_folder(
        self, name: str, *, parent_id: str | None
    ) -> str:
        drive_prefix = (
            f"{GRAPH_BASE_URL}/drives/{quote(self.settings.drive_id, safe='')}"
        )
        if parent_id is None:
            url = f"{drive_prefix}/root/children"
        else:
            url = f"{drive_prefix}/items/{quote(parent_id, safe='')}/children"

        existing_id = self._find_child_folder(url, name)
        if existing_id is not None:
            return existing_id

        # Folder does not exist — create it. "fail" rather than "rename": if
        # another process (web app or worker) creates it first, use theirs
        # instead of silently filing into "Paid 1".
        payload = {
            "name": name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        }
        created = self.session.request(
            "POST",
            url,
            operation="SharePoint request",
            json=payload,
            ok_statuses={409},
        )
        with self._cache_lock:
            self._folder_inventory_cache.clear()
        if created.status_code == 409:
            existing_id = self._find_child_folder(url, name)
            if existing_id is None:
                raise SharePointError(
                    f"SharePoint folder '{name}' could not be created: an item "
                    "with that name already exists."
                )
            return existing_id
        return str(created.json()["id"])

    def _find_child_folder(self, children_url: str, name: str) -> str | None:
        for payload in self.session.paginate(
            children_url,
            params={"$select": "id,name,folder", "$top": "200"},
            operation="SharePoint folder lookup",
        ):
            items = payload.get("value", [])
            if not isinstance(items, list):
                raise SharePointError(
                    "SharePoint returned an invalid folder lookup response."
                )
            for item in items:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("folder"), dict)
                    and str(item.get("name", "")).casefold() == name.casefold()
                ):
                    return str(item["id"])
        return None

    def _send_json(
        self,
        method: str,
        url: str,
        payload: dict[str, Any],
        *,
        idempotent: bool = False,
    ) -> httpx.Response:
        return self.session.request(
            method,
            url,
            operation="SharePoint request",
            json=payload,
            idempotent=idempotent,
        )

def sharepoint_settings_from_environment() -> SharePointSettings:
    return SharePointSettings(
        site_id=os.environ.get("SHAREPOINT_SITE_ID", ""),
        drive_id=os.environ.get("SHAREPOINT_DRIVE_ID", ""),
        incoming_folder=os.environ.get(
            "SHAREPOINT_INCOMING_FOLDER", "Invoices/Incoming Invoices"
        ),
        flagged_folder=os.environ.get(
            "SHAREPOINT_FLAGGED_FOLDER", FLAGGED_INVOICES_FOLDER
        ),
    )


def sharepoint_client_from_environment() -> SharePointClient:
    from app.outlook_graph import settings_from_environment

    return SharePointClient(
        sharepoint_settings_from_environment(),
        settings_from_environment(),
    )
