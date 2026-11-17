"""Shared Microsoft Graph HTTP helpers.

One place for: bearer-token requests, raising the caller's own error type with
the Graph request ID, retry with exponential backoff + jitter (honouring
``Retry-After``), ``@odata.nextLink`` pagination and size-bounded streaming
downloads.

Retry safety is decided per call site, not per HTTP method:

* ``idempotent=True``  -> retried on 429/500/502/503/504, connect errors and
  read/connect timeouts (safe for GET, DELETE, PATCH-move, PUT to a fixed path
  with ``replace`` semantics).
* ``idempotent=False`` -> retried ONLY when Graph guarantees the request was not
  processed: HTTP 429 (throttled before execution) and connection failures
  where the request never left the client (``ConnectError``/``ConnectTimeout``).
"""

from __future__ import annotations

import email.utils
import logging
import random
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Iterator, Mapping

import httpx
import msal


logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
DEFAULT_BACKOFF_MAX_SECONDS = 30.0
DEFAULT_RETRY_AFTER_CAP_SECONDS = 60.0

# Errors where the request provably never reached the server.
_NOT_SENT_ERRORS: tuple[type[Exception], ...] = (httpx.ConnectError, httpx.ConnectTimeout)
# Errors where the request may or may not have been processed.
_IDEMPOTENT_ONLY_ERRORS: tuple[type[Exception], ...] = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)


def _default_sleep(seconds: float) -> None:
    time.sleep(seconds)


# Module-level hook so the test suite can disable real sleeping globally.
sleep_function: Callable[[float], None] = _default_sleep


class MsalClientCredentialTokenProvider:
    """Client-credential token provider for Microsoft Graph.

    MSAL caches tokens in-process, so sharing one instance across clients means
    one token is reused instead of each client fetching its own.
    """

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        *,
        error_factory: Callable[[str], Exception] = RuntimeError,
    ) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._application: msal.ConfidentialClientApplication | None = None
        self._error_factory = error_factory
        self._lock = threading.Lock()

    @property
    def application(self) -> msal.ConfidentialClientApplication:
        # Built lazily: MSAL performs authority discovery over the network when
        # the application is constructed.
        if self._application is None:
            self._application = msal.ConfidentialClientApplication(
                self._client_id,
                authority=f"https://login.microsoftonline.com/{self._tenant_id}",
                client_credential=self._client_secret,
            )
        return self._application

    def __call__(self) -> str:
        with self._lock:
            result = self.application.acquire_token_for_client(scopes=[GRAPH_SCOPE])
        token = result.get("access_token")
        if not isinstance(token, str):
            description = result.get("error_description", "Unknown token error.")
            raise self._error_factory(f"Unable to obtain Graph token: {description}")
        return token


@lru_cache(maxsize=8)
def shared_token_provider(
    tenant_id: str,
    client_id: str,
    client_secret: str,
    error_factory: Callable[[str], Exception] = RuntimeError,
) -> MsalClientCredentialTokenProvider:
    """Return one token provider per credential set for the whole process."""
    return MsalClientCredentialTokenProvider(
        tenant_id, client_id, client_secret, error_factory=error_factory
    )


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Return the Retry-After delay in seconds (delta-seconds or HTTP-date)."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (when - current).total_seconds())


class GraphSession:
    """Authenticated, retrying Microsoft Graph HTTP session.

    ``error_class`` is the caller's own exception type; every HTTP failure is
    raised as ``error_class(f"{operation} failed with HTTP {code}; request ID: {id}.")``.
    """

    def __init__(
        self,
        http_client: httpx.Client,
        token_provider: Callable[[], str],
        *,
        error_class: type[Exception],
        base_url: str = GRAPH_BASE_URL,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
        retry_after_cap_seconds: float = DEFAULT_RETRY_AFTER_CAP_SECONDS,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        self.http_client = http_client
        self.token_provider = token_provider
        self.error_class = error_class
        self.base_url = base_url
        self.max_retries = max(0, max_retries)
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.retry_after_cap_seconds = retry_after_cap_seconds
        self._sleep = sleep
        self._jitter = jitter or (lambda ceiling: random.uniform(0.0, ceiling))

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("https://", "http://")):
            return path_or_url
        return f"{self.base_url}{path_or_url}"

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        operation: str = "Microsoft Graph request",
        idempotent: bool | None = None,
        ok_statuses: frozenset[int] | set[int] = frozenset(),
        ok_on_retry_statuses: frozenset[int] | set[int] = frozenset(),
        params: Mapping[str, str] | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        follow_redirects: bool | None = None,
        raise_for_status: bool = True,
    ) -> httpx.Response:
        """Send one request with retries and return the (successful) response."""
        if idempotent is None:
            idempotent = method.upper() in {"GET", "HEAD", "DELETE", "OPTIONS"}
        url = self.url(path_or_url)
        attempt = 0
        while True:
            request_headers = {"Authorization": f"Bearer {self.token_provider()}"}
            if json is not None:
                request_headers["Content-Type"] = "application/json"
            if headers:
                request_headers.update(headers)
            kwargs: dict[str, Any] = {
                "params": params,
                "headers": request_headers,
            }
            if json is not None:
                kwargs["json"] = json
            if content is not None:
                kwargs["content"] = content
            if follow_redirects is not None:
                kwargs["follow_redirects"] = follow_redirects
            try:
                response = self.http_client.request(method, url, **kwargs)
            except Exception as error:
                if self._should_retry_exception(error, idempotent, attempt):
                    self._wait(attempt, None, method, url, repr(error))
                    attempt += 1
                    continue
                raise
            if response.is_success or response.status_code in ok_statuses:
                return response
            if attempt > 0 and response.status_code in ok_on_retry_statuses:
                # e.g. a DELETE retried after a lost success now returns 404.
                return response
            if self._should_retry_status(response.status_code, idempotent, attempt):
                self._wait(
                    attempt,
                    response.headers.get("Retry-After"),
                    method,
                    url,
                    f"HTTP {response.status_code}",
                )
                attempt += 1
                continue
            if raise_for_status:
                self.raise_error(response, operation)
            return response

    def get_json(
        self,
        path_or_url: str,
        *,
        operation: str = "Microsoft Graph request",
        params: Mapping[str, str] | None = None,
    ) -> Any:
        return self.request("GET", path_or_url, operation=operation, params=params).json()

    def paginate(
        self,
        path_or_url: str,
        *,
        operation: str = "Microsoft Graph request",
        params: Mapping[str, str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield each page payload, following ``@odata.nextLink``.

        ``params`` are sent only on the first request; nextLink already
        contains the query string.
        """
        url: str | None = path_or_url
        page_params = params
        while url:
            payload = self.get_json(url, operation=operation, params=page_params)
            page_params = None
            if not isinstance(payload, dict):
                payload = {}
            yield payload
            next_link = payload.get("@odata.nextLink")
            url = next_link if isinstance(next_link, str) and next_link else None

    def download(
        self,
        path_or_url: str,
        *,
        max_bytes: int,
        too_large_error: Callable[[], Exception],
        operation: str = "Microsoft Graph request",
        params: Mapping[str, str] | None = None,
        follow_redirects: bool | None = None,
        chunk_size: int = 64 * 1024,
    ) -> bytes:
        """Stream a GET body, stopping as soon as it exceeds ``max_bytes``.

        Size errors are never retried.
        """
        url = self.url(path_or_url)
        attempt = 0
        while True:
            kwargs: dict[str, Any] = {
                "params": params,
                "headers": {"Authorization": f"Bearer {self.token_provider()}"},
            }
            if follow_redirects is not None:
                kwargs["follow_redirects"] = follow_redirects
            try:
                with self.http_client.stream("GET", url, **kwargs) as response:
                    if not response.is_success:
                        response.close()
                        if self._should_retry_status(response.status_code, True, attempt):
                            retry_after = response.headers.get("Retry-After")
                            status = response.status_code
                        else:
                            self.raise_error(response, operation)
                    else:
                        declared = response.headers.get("Content-Length")
                        if declared is not None:
                            try:
                                if int(declared) > max_bytes:
                                    raise too_large_error()
                            except ValueError:
                                pass
                        chunks: list[bytes] = []
                        total = 0
                        for chunk in response.iter_bytes(chunk_size):
                            total += len(chunk)
                            if total > max_bytes:
                                raise too_large_error()
                            chunks.append(chunk)
                        return b"".join(chunks)
            except Exception as error:
                if isinstance(error, self.error_class) or not self._should_retry_exception(
                    error, True, attempt
                ):
                    raise
                self._wait(attempt, None, "GET", url, repr(error))
                attempt += 1
                continue
            # Retryable HTTP status (body was not consumed).
            self._wait(attempt, retry_after, "GET", url, f"HTTP {status}")
            attempt += 1

    def raise_error(self, response: httpx.Response, operation: str) -> None:
        request_id = response.headers.get("request-id", "not provided")
        raise self.error_class(
            f"{operation} failed with HTTP {response.status_code}; "
            f"request ID: {request_id}."
        )

    # ------------------------------------------------------------------
    # Retry policy
    # ------------------------------------------------------------------

    def _should_retry_status(self, status: int, idempotent: bool, attempt: int) -> bool:
        if attempt >= self.max_retries:
            return False
        if status == 429:
            return True
        return idempotent and status in RETRYABLE_STATUS_CODES

    def _should_retry_exception(
        self, error: Exception, idempotent: bool, attempt: int
    ) -> bool:
        if attempt >= self.max_retries:
            return False
        if isinstance(error, _NOT_SENT_ERRORS):
            return True
        return idempotent and isinstance(error, _IDEMPOTENT_ONLY_ERRORS)

    def backoff_seconds(self, attempt: int, retry_after: str | None) -> float:
        explicit = parse_retry_after(retry_after)
        if explicit is not None:
            return min(explicit, self.retry_after_cap_seconds)
        ceiling = min(
            self.backoff_max_seconds, self.backoff_base_seconds * (2 ** attempt)
        )
        # Equal jitter: at least half the exponential step, plus random spread.
        return ceiling / 2 + self._jitter(ceiling / 2)

    def _wait(
        self,
        attempt: int,
        retry_after: str | None,
        method: str,
        url: str,
        reason: str,
    ) -> None:
        delay = self.backoff_seconds(attempt, retry_after)
        logger.warning(
            "Graph %s %s failed (%s); retry %d/%d in %.2fs.",
            method,
            url.split("?", 1)[0],
            reason,
            attempt + 1,
            self.max_retries,
            delay,
        )
        sleeper = self._sleep if self._sleep is not None else sleep_function
        sleeper(delay)
