from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlencode

import msal

from app.auth import (
    AuthError,
    ROLE_ADMIN,
    ROLE_APPROVER_1,
    ROLE_APPROVER_2,
    ROLE_PURCHASE_LEDGER,
    ROLE_PURCHASING,
    User,
)


APP_ROLE_TO_INTERNAL_ROLE = {
    "InvoiceProcessor.Admin": ROLE_ADMIN,
    "InvoiceProcessor.PurchaseLedger": ROLE_PURCHASE_LEDGER,
    "InvoiceProcessor.Approver1": ROLE_APPROVER_1,
    "InvoiceProcessor.Approver2": ROLE_APPROVER_2,
    "InvoiceProcessor.Purchasing": ROLE_PURCHASING,
}


@dataclass(frozen=True)
class EntraAuthSettings:
    tenant_id: str
    client_id: str
    client_secret: str
    redirect_uri: str
    post_logout_redirect_uri: str

    @classmethod
    def from_environment(cls) -> EntraAuthSettings | None:
        values = {
            "tenant_id": os.environ.get("AUTH_ENTRA_TENANT_ID", "").strip(),
            "client_id": os.environ.get("AUTH_ENTRA_CLIENT_ID", "").strip(),
            "client_secret": os.environ.get(
                "AUTH_ENTRA_CLIENT_SECRET", ""
            ).strip(),
            "redirect_uri": os.environ.get(
                "AUTH_ENTRA_REDIRECT_URI", ""
            ).strip(),
        }
        if not any(values.values()):
            return None
        missing = [name for name, value in values.items() if not value]
        if missing:
            names = ", ".join(f"AUTH_ENTRA_{name.upper()}" for name in missing)
            raise AuthError(f"Microsoft Entra authentication is missing: {names}.")
        post_logout = os.environ.get(
            "AUTH_ENTRA_POST_LOGOUT_REDIRECT_URI", ""
        ).strip()
        if not post_logout:
            post_logout = values["redirect_uri"].rsplit(
                "/api/auth/microsoft/callback", 1
            )[0] + "/"
        return cls(
            **values,
            post_logout_redirect_uri=post_logout,
        )


class EntraAuthClient:
    def __init__(self, settings: EntraAuthSettings) -> None:
        self.settings = settings
        self._client = msal.ConfidentialClientApplication(
            settings.client_id,
            authority=(
                "https://login.microsoftonline.com/"
                f"{settings.tenant_id}"
            ),
            client_credential=settings.client_secret,
        )

    def initiate_flow(self) -> dict[str, object]:
        flow = self._client.initiate_auth_code_flow(
            scopes=[],
            redirect_uri=self.settings.redirect_uri,
        )
        if "auth_uri" not in flow or "state" not in flow:
            raise AuthError("Microsoft Entra did not return a valid login flow.")
        return flow

    def complete_flow(
        self,
        flow: dict[str, object],
        query_parameters: Mapping[str, str],
    ) -> User:
        try:
            result = self._client.acquire_token_by_auth_code_flow(
                flow,
                dict(query_parameters),
            )
        except ValueError as error:
            raise AuthError(
                "Microsoft sign-in validation failed. Please start again."
            ) from error
        if "error" in result:
            description = str(
                result.get("error_description")
                or result.get("error")
                or "Microsoft sign-in failed."
            )
            raise AuthError(description)
        claims = result.get("id_token_claims")
        if not isinstance(claims, dict):
            raise AuthError("Microsoft sign-in returned no identity claims.")
        assigned = {
            APP_ROLE_TO_INTERNAL_ROLE[value]
            for value in claims.get("roles", [])
            if value in APP_ROLE_TO_INTERNAL_ROLE
        }
        if not assigned:
            raise AuthError(
                "Your Microsoft 365 account has not been assigned an Invoice "
                "Processor application role."
            )
        if len(assigned) > 1:
            raise AuthError(
                "Your Microsoft 365 account has multiple Invoice Processor "
                "roles. Ask an administrator to assign exactly one."
            )
        username = str(
            claims.get("preferred_username")
            or claims.get("email")
            or claims.get("oid")
            or ""
        ).strip()
        if not username:
            raise AuthError("Microsoft sign-in returned no user identifier.")
        display_name = str(claims.get("name") or username).strip()
        email = str(
            claims.get("email")
            or claims.get("preferred_username")
            or ""
        ).strip() or None
        return User(
            username=username,
            display_name=display_name,
            email=email,
            role=assigned.pop(),
        )

    def logout_url(self) -> str:
        return (
            "https://login.microsoftonline.com/"
            f"{self.settings.tenant_id}/oauth2/v2.0/logout?"
            + urlencode(
                {
                    "post_logout_redirect_uri": (
                        self.settings.post_logout_redirect_uri
                    )
                }
            )
        )


def entra_auth_client_from_environment() -> EntraAuthClient | None:
    settings = EntraAuthSettings.from_environment()
    return EntraAuthClient(settings) if settings is not None else None
