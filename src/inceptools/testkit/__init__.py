"""The real pipeline, in memory, with the envelope decoded.

A test here goes through the real middleware, the real guards and the real
exception filter — which is where most of this framework's behaviour lives,
and where a mock would quietly diverge. The thing you most want to test is
that an endpoint is *refused*, and a mock that bypasses the guard tests
nothing.

    def test_create_requires_an_account(client):
        client.post("/api/v1/catalog/products", {"name": "ab"}) \\
              .expect_status(401).expect_code("NO_TENANT")
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping
from typing import Any

from inceptools.application import Application, Options, create_app
from inceptools.core import ctxkit
from inceptools.web import middleware as middleware_mod

__all__ = [
    "Reply",
    "TestApp",
    "TestingModule",
    "acting_as",
    "build",
    "build_async",
    "client_for",
    "identity_from_headers",
]

#: The headers :func:`build` reads identity from, so a test can act as an
#: account without standing up an auth server.
ACCOUNT_HEADER = "x-test-account"
ACTOR_HEADER = "x-test-actor"
ROLES_HEADER = "x-test-roles"
SCOPES_HEADER = "x-test-scopes"


def identity_from_headers(request: Any) -> dict[str, Any]:
    """A resolver that trusts test headers. Never wire this into a real service."""
    headers = request.headers
    roles = [r.strip() for r in headers.get(ROLES_HEADER, "").split(",") if r.strip()]
    scopes = [s.strip() for s in headers.get(SCOPES_HEADER, "").split(",") if s.strip()]
    return {
        "account_id": headers.get(ACCOUNT_HEADER) or None,
        "actor_id": headers.get(ACTOR_HEADER) or None,
        "roles": frozenset(roles),
        "scopes": frozenset(scopes),
    }


class Reply:
    """One response, with the envelope already unpacked.

    Every assertion returns ``self``, so expectations chain and a failure
    message names the whole reply rather than one field of it.
    """

    __slots__ = ("_body", "response")

    def __init__(self, response: Any) -> None:
        self.response = response
        try:
            self._body = response.json()
        except Exception:
            self._body = None

    # -- accessors -------------------------------------------------------

    @property
    def status(self) -> int:
        return self.response.status_code

    @property
    def body(self) -> Any:
        return self._body

    @property
    def meta(self) -> dict[str, Any]:
        if isinstance(self._body, dict):
            found = self._body.get("meta")
            if isinstance(found, dict):
                return found
            # The flat envelope keeps these at the top level.
            return {k: v for k, v in self._body.items() if k != "data"}
        return {}

    @property
    def data(self) -> Any:
        return self._body.get("data") if isinstance(self._body, dict) else self._body

    @property
    def code(self) -> str:
        return str(self.meta.get("code", ""))

    @property
    def message(self) -> str:
        return str(self.meta.get("message", ""))

    @property
    def errors(self) -> list[dict[str, Any]]:
        found = self.meta.get("errors")
        return found if isinstance(found, list) else []

    @property
    def fields(self) -> list[str]:
        """The field names a validation failure complained about."""
        return [str(e.get("field", "")) for e in self.errors]

    def header(self, name: str, default: str = "") -> str:
        return self.response.headers.get(name, default)

    # -- expectations ----------------------------------------------------

    def expect_status(self, expected: int) -> Reply:
        assert self.status == expected, (
            f"expected status {expected}, got {self.status}: {self._body!r}"
        )
        return self

    def expect_ok(self) -> Reply:
        assert 200 <= self.status < 300, f"expected success, got {self.status}: {self._body!r}"
        assert self.meta.get("success") is True, f"meta.success is not true: {self.meta!r}"
        return self

    def expect_failure(self) -> Reply:
        assert self.status >= 400, f"expected a failure, got {self.status}"
        assert self.meta.get("success") is False, f"meta.success is not false: {self.meta!r}"
        assert self.data is None, f"a failure carried data: {self.data!r}"
        return self

    def expect_code(self, expected: str) -> Reply:
        assert self.code == expected, f"expected code {expected!r}, got {self.code!r}"
        return self

    def expect_message(self, expected: str) -> Reply:
        assert self.message == expected, f"expected message {expected!r}, got {self.message!r}"
        return self

    def expect_field_error(self, field: str) -> Reply:
        assert field in self.fields, f"expected an error on {field!r}, got {self.fields}"
        return self

    def expect_data(self, expected: Any) -> Reply:
        assert self.data == expected, f"expected data {expected!r}, got {self.data!r}"
        return self

    def expect_enveloped(self) -> Reply:
        assert isinstance(self._body, dict), f"body is not an object: {self._body!r}"
        assert "data" in self._body, f"body has no 'data' key: {sorted(self._body)}"
        assert self.meta, "body has no meta"
        return self

    def expect_header(self, name: str, expected: str | None = None) -> Reply:
        value = self.header(name)
        assert value, f"expected header {name!r}, got none"
        if expected is not None:
            assert value == expected, f"header {name!r} is {value!r}, expected {expected!r}"
        return self

    def __repr__(self) -> str:
        return f"<Reply {self.status} {self.code or ''} {self._body!r}>"


class TestApp:
    """An application plus a client that speaks envelopes."""

    def __init__(self, app: Application) -> None:
        self.app = app
        self._client: Any = None
        self._stack: contextlib.ExitStack | None = None
        self._headers: dict[str, str] = {}

    # -- lifecycle -------------------------------------------------------

    def __enter__(self) -> TestApp:
        from fastapi.testclient import TestClient

        self._stack = contextlib.ExitStack()
        self._client = self._stack.enter_context(TestClient(self.app))
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._stack is not None:
            self._stack.close()
        self._stack = None
        self._client = None

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RuntimeError("use TestApp as a context manager, or call build(...) in a fixture")
        return self._client

    @property
    def routes(self) -> Any:
        return self.app.routes

    @property
    def container(self) -> Any:
        """The application, which can find a provider in any module container."""
        return self.app

    # -- acting as somebody ----------------------------------------------

    def as_account(self, account_id: str, **extra: str) -> TestApp:
        """Send every subsequent request as *account_id*."""
        self._headers[ACCOUNT_HEADER] = account_id
        for key, value in extra.items():
            self._headers[key] = value
        return self

    def as_actor(
        self,
        actor_id: str,
        *,
        account_id: str = "",
        roles: str = "",
        scopes: str = "",
    ) -> TestApp:
        self._headers[ACTOR_HEADER] = actor_id
        if account_id:
            self._headers[ACCOUNT_HEADER] = account_id
        if roles:
            self._headers[ROLES_HEADER] = roles
        if scopes:
            self._headers[SCOPES_HEADER] = scopes
        return self

    def anonymous(self) -> TestApp:
        for key in (ACCOUNT_HEADER, ACTOR_HEADER, ROLES_HEADER, SCOPES_HEADER):
            self._headers.pop(key, None)
        return self

    @contextlib.contextmanager
    def acting_as(self, **identity: Any) -> Iterator[TestApp]:
        """Act as somebody for the duration of a block, then revert."""
        previous = dict(self._headers)
        mapping = {
            "account_id": ACCOUNT_HEADER,
            "actor_id": ACTOR_HEADER,
            "roles": ROLES_HEADER,
            "scopes": SCOPES_HEADER,
        }
        for key, value in identity.items():
            header = mapping.get(key)
            if header:
                self._headers[header] = (
                    ",".join(value) if isinstance(value, list | tuple | set) else str(value)
                )
        try:
            yield self
        finally:
            self._headers = previous

    # -- requests --------------------------------------------------------

    def _merged(self, headers: Mapping[str, str] | None) -> dict[str, str]:
        merged = dict(self._headers)
        merged.update(headers or {})
        return merged

    def get(self, path: str, *, headers: Mapping[str, str] | None = None, **kwargs: Any) -> Reply:
        return Reply(self.client.get(path, headers=self._merged(headers), **kwargs))

    def post(
        self,
        path: str,
        json: Any = None,
        *,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> Reply:
        return Reply(self.client.post(path, json=json, headers=self._merged(headers), **kwargs))

    def put(self, path: str, json: Any = None, **kwargs: Any) -> Reply:
        headers = self._merged(kwargs.pop("headers", None))
        return Reply(self.client.put(path, json=json, headers=headers, **kwargs))

    def patch(self, path: str, json: Any = None, **kwargs: Any) -> Reply:
        headers = self._merged(kwargs.pop("headers", None))
        return Reply(self.client.patch(path, json=json, headers=headers, **kwargs))

    def delete(self, path: str, **kwargs: Any) -> Reply:
        headers = self._merged(kwargs.pop("headers", None))
        return Reply(self.client.delete(path, headers=headers, **kwargs))

    # -- assertions about the application itself -------------------------

    def expect_all_writes_guarded(self) -> TestApp:
        """The cheapest security control in the framework.

        The failure it catches — a write endpoint shipped without
        authentication — is both easy to commit and expensive to find.
        """
        unguarded = self.app.routes.unguarded()
        assert not unguarded, "mutating routes with no guard: " + ", ".join(
            f"{m.route.method} {m.full_path}" for m in unguarded
        )
        return self


def build(*apps: Any, options: Options | None = None, **overrides: Any) -> TestApp:
    """An application wired for tests.

    Logging is off, identity comes from test headers, and the health routes
    are present so a probe test works like production.
    """
    if options is None:
        defaults: dict[str, Any] = {
            "service": "test",
            "configure_logging": False,
            "middleware": middleware_mod.ChainOptions(identity=identity_from_headers),
        }
        defaults.update(overrides)
        options = Options(**defaults)

    app = create_app(options)
    if apps:
        app.mount(*apps)
    return TestApp(app)


async def build_async(*apps: Any, options: Options | None = None, **overrides: Any) -> TestApp:
    """``build`` for a test that is already inside an event loop."""
    test_app = build(options=options, **overrides)
    if apps:
        await test_app.app.mount_async(*apps)
    return test_app


@contextlib.contextmanager
def client_for(*apps: Any, **overrides: Any) -> Iterator[TestApp]:
    """``with testkit.client_for(CatalogApp) as client: ...``"""
    with build(*apps, **overrides) as test_app:
        yield test_app


@contextlib.contextmanager
def acting_as(**identity: Any) -> Iterator[None]:
    """Set request identity directly, for testing a service without HTTP."""
    with ctxkit.use(**identity):
        yield


def __getattr__(name: str) -> Any:
    """Expose ``TestingModule`` lazily, to avoid a circular import at module load."""
    if name == "TestingModule":
        from inceptools.testkit.module import TestingModule

        return TestingModule
    raise AttributeError(name)
