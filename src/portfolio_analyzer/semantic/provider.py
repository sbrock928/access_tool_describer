"""Local OpenAI-compatible semantic provider with constrained JSON output."""

from __future__ import annotations

import json
import socket
import time
from collections.abc import Sequence
from ipaddress import ip_address
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from portfolio_analyzer.semantic.config import ModelEndpointSettings, SemanticSettings


class SemanticProviderError(RuntimeError):
    pass


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise SemanticProviderError(
            f"Local semantic endpoint attempted an HTTP redirect ({code}); redirects are disabled"
        )


class SemanticProvider(Protocol):
    def health(self) -> dict[str, str | None]: ...

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class LocalOpenAIProvider:
    """HTTP adapter for separately managed, local model servers."""

    def __init__(self, settings: SemanticSettings) -> None:
        self.settings = settings
        _require_loopback(settings.chat.base_url)
        _require_loopback(settings.embeddings.base_url)
        # Ignore process-level proxy configuration so a local request can never be
        # forwarded to an external proxy, even on a misconfigured workstation.
        self._opener = build_opener(ProxyHandler({}), _RejectRedirects())

    def health(self) -> dict[str, str | None]:
        chat = self._request(self.settings.chat, "GET", "/models")
        embeddings = self._request(self.settings.embeddings, "GET", "/models")
        return {
            "chat_model": self.settings.chat.model,
            "embedding_model": self.settings.embeddings.model,
            "chat_server": _server_identity(chat),
            "embedding_server": _server_identity(embeddings),
        }

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        body = {
            "model": self.settings.chat.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.settings.execution.temperature,
            "max_tokens": self.settings.execution.max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "schema": schema,
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
        }
        response = self._request(self.settings.chat, "POST", "/chat/completions", body)
        try:
            content = response["choices"][0]["message"]["content"]
            if isinstance(content, dict):
                return content
            parsed = json.loads(str(content))
            if not isinstance(parsed, dict):
                raise TypeError("structured output must be an object")
            return parsed
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise SemanticProviderError("Local model returned invalid structured output") from exc

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self._request(
            self.settings.embeddings,
            "POST",
            "/embeddings",
            {"model": self.settings.embeddings.model, "input": list(texts)},
        )
        try:
            items = sorted(response["data"], key=lambda item: int(item["index"]))
            vectors = [[float(value) for value in item["embedding"]] for item in items]
        except (KeyError, TypeError, ValueError) as exc:
            raise SemanticProviderError("Local embedding server returned invalid vectors") from exc
        if len(vectors) != len(texts) or any(not vector for vector in vectors):
            raise SemanticProviderError("Local embedding server returned the wrong vector count")
        return vectors

    def _request(
        self,
        endpoint: ModelEndpointSettings,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{endpoint.base_url.rstrip('/')}{path}"
        _require_loopback(url)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        for attempt in range(endpoint.max_retries + 1):
            try:
                with self._opener.open(request, timeout=endpoint.timeout_seconds) as response:
                    value = json.load(response)
                if not isinstance(value, dict):
                    raise SemanticProviderError(f"Unexpected response from {url}")
                return value
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt >= endpoint.max_retries:
                    raise SemanticProviderError(
                        f"Local model request failed: {url}: {exc}"
                    ) from exc
                time.sleep(min(2**attempt, 4))
        raise AssertionError("unreachable")


def _require_loopback(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Semantic endpoint must be loopback-only: {url}")
    hostname = parsed.hostname.casefold()
    if hostname != "localhost":
        try:
            if ip_address(hostname).is_loopback:
                return
        except ValueError:
            pass
        raise ValueError(f"Semantic endpoint must be loopback-only: {url}")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname,
                parsed.port or 80,
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise ValueError(f"Semantic endpoint must be loopback-only: {url}") from exc
    if not addresses or any(not ip_address(value).is_loopback for value in addresses):
        raise ValueError(f"Semantic endpoint must resolve only to loopback addresses: {url}")


def _server_identity(response: dict[str, Any]) -> str | None:
    data = response.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    value = data[0].get("owned_by") or data[0].get("id")
    return str(value) if value is not None else None
