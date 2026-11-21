"""
http_util.py — what the photo app's HTTP servers share (standard library only): a response as a value,
bounded JSON bodies, query parameters that answer 400 instead of failing, and who may write.

  Response(status, body, content_type, cache, headers)   frozen; Response.of(module tuple | None) -> Response
  RouteAnswer                                            the type of what Response.of takes (a route module's answer)
  json_response(obj, status=200) -> Response             application/json, no-store
  NOT_FOUND                                              404 text/plain
  HTTPError(status, message)                             raised by the parsers below; .response is a JSON {"error"}
  Request(method, target, headers, body, local)          one request, as PhotoApp.handle takes it
  Headers                                                what Request.headers is: a dict or the handler's Message
  content_length(headers) -> int                         0 when absent; HTTPError 400 when not a whole number
  read_body(rfile, headers, limit=MAX_BODY) -> bytes     the body; 413 over `limit` (left unread: close the connection)
  parse_json(raw) -> object                              an empty body is {}; HTTPError 400 when it is not JSON
  read_json(rfile, headers, limit=MAX_BODY) -> object    read_body, then parse_json
  int_param(qs, name, default, min=, max=) -> int        a whole number, clamped to [min, max]; 400 on garbage
  byte_range(header, size) -> (first, last) | None       a Range header's one range, for a file of `size` bytes
  Auth.PUBLIC | Auth.WRITER | Auth.LOCAL_OR_WRITER       who may make a request; Auth.allows(writer=, local=)

Who may write. `--upload-user` names the Tailscale logins that may change the library (`writer`: every
caller, when it names none). A request `local` comes from this computer itself, not through
`tailscale serve` (intake/server.py's is_local): that is where the headless agent (tasks.py) and an MCP
client on the same machine run, and whoever is there can already change the catalog's files. Every
request that changes the library is LOCAL_OR_WRITER; reading is PUBLIC. WRITER (a listed login only) is
here for a route that must not be open to this computer's own callers; none needs it today.

Phone uploads stream to disk and are not JSON: they never go through read_body (admin.py has its own
limits). MAX_BODY is for the JSON bodies every other POST takes: a change set or album of a few
thousand photos with their reasons is well under it.
"""
from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol, TypeAlias

MAX_BODY = 8 * 1024 * 1024     # bytes of JSON a POST may carry


@dataclass(frozen=True)
class Response:
    """What a route answers. `headers` are extra headers (an "ETag" among them makes the answer
    revalidatable: the handler answers 304 to a matching If-None-Match). Immutable, headers included."""
    status: int
    body: bytes = b""
    content_type: str = "text/plain"
    cache: str = "no-store"
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))

    @property
    def etag(self) -> str | None:
        return self.headers.get("ETag")

    @classmethod
    def of(cls, got: RouteAnswer) -> Response:
        """A module's answer: a Response, or (status, body, content type[, Cache-Control[, headers]]).
        None (not ours) is a 404."""
        if isinstance(got, Response):
            return got
        if got is None:
            return NOT_FOUND
        match got:
            case (code, body, ctype):
                return cls(code, body, ctype)
            case (code, body, ctype, cache):
                return cls(code, body, ctype, cache)
            case (code, body, ctype, cache, headers):
                return cls(code, body, ctype, cache, dict(headers))
        raise ValueError(f"not a route's answer: a tuple of {len(got)}")

    def json(self) -> Any:
        """The body, read as JSON (for tests)."""
        return json.loads(self.body) if self.body else None


# What a route module's handle() answers, before Response.of makes it a Response: a Response, None (not
# this module's route), or the legacy tuple (status, body, content type[, Cache-Control[, headers]]).
RouteAnswer: TypeAlias = (Response | tuple[int, bytes, str] | tuple[int, bytes, str, str]
                          | tuple[int, bytes, str, str, Mapping[str, str]] | None)


def json_response(obj, status: int = 200, headers: Mapping[str, str] | None = None) -> Response:
    return Response(status, json.dumps(obj, separators=(",", ":")).encode(), "application/json", "no-store",
                    headers or {})


NOT_FOUND = Response(404, b"not found", "text/plain", "no-store")


class HTTPError(Exception):
    """A request that can't be answered as asked: 400 (malformed), 403 (not allowed), 413 (too large)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message

    @property
    def response(self) -> Response:
        return json_response({"error": self.message}, self.status)


FORBIDDEN = HTTPError(403, "not allowed").response


class Headers(Protocol):
    """A request's headers: a dict (tests, PhotoApp.request) or the handler's email.message.Message
    (whose get() ignores case)."""
    def get(self, name: str, default: Any = None, /) -> Any: ...
    def keys(self) -> Iterable[str]: ...
    def __getitem__(self, name: str, /) -> Any: ...


@dataclass(frozen=True)
class Request:
    """One request as PhotoApp.handle takes it. `target` is the path with its query string; `local` is
    is_local() of the connection it came on (see the module docstring)."""
    method: str
    target: str
    headers: Headers = field(default_factory=dict)
    body: bytes = b""
    local: bool = False


def content_length(headers) -> int:
    raw = (headers.get("Content-Length") or "").strip()
    if not raw:
        return 0
    if not (raw.isascii() and raw.isdigit()):    # also refuses "-1", "1e3" and "²"
        raise HTTPError(400, "Content-Length must be a whole number")
    return int(raw)


def read_body(rfile, headers, limit: int = MAX_BODY) -> bytes:
    """The request's body, at most `limit` bytes. Over it, HTTPError 413 with nothing read: the caller
    must close the connection, or the unread body would be taken for the next request."""
    n = content_length(headers)
    if n > limit:
        raise HTTPError(413, f"the body is over {limit} bytes")
    return rfile.read(n) if n else b""


def parse_json(raw: bytes):
    """A JSON body. An empty one is {} (a POST that needs no arguments sends none)."""
    if not raw or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError:               # JSONDecodeError and UnicodeDecodeError
        raise HTTPError(400, "the body is not JSON") from None


def read_json(rfile, headers, limit: int = MAX_BODY):
    return parse_json(read_body(rfile, headers, limit))


def int_param(qs: Mapping, name: str, default: int, *, min: int | None = None, max: int | None = None) -> int:
    """`qs` as parse_qs gives it ({name: [values]}). Absent or empty is `default`; otherwise a whole
    number (optionally signed), clamped to [min, max]; anything else is HTTPError 400."""
    v = qs.get(name)
    raw = (v[0] if isinstance(v, (list, tuple)) else v) if v else ""
    raw = str(raw).strip()
    if not raw:
        n = default
    else:
        try:
            n = int(raw)
        except ValueError:
            raise HTTPError(400, f"{name} must be a whole number") from None
    if min is not None and n < min:
        n = min
    if max is not None and n > max:
        n = max
    return n


def byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The bytes a `Range` header asks for, as (first, last), both inclusive and inside the file. None when
    there is no header, or it is one this server answers with the whole file (not `bytes=`, several ranges,
    or not numbers: RFC 9110 lets a server ignore a Range it does not understand). ValueError when it is
    well formed and cannot be satisfied (it starts at or after the end, or asks for the last 0 bytes): a 416."""
    if not header or not header.strip().lower().startswith("bytes="):
        return None
    spec = header.strip()[6:].strip()
    first, dash, last = spec.partition("-")
    first, last = first.strip(), last.strip()
    if "," in spec or not dash or not ((first == "" or first.isascii() and first.isdigit())
                                       and (last == "" or last.isascii() and last.isdigit())) or (first == last == ""):
        return None
    if first == "":                      # the last N bytes
        n = int(last)
        if n == 0 or size == 0:
            raise ValueError("unsatisfiable")
        return max(0, size - n), size - 1
    start = int(first)
    if start >= size:
        raise ValueError("unsatisfiable")
    end = min(int(last), size - 1) if last else size - 1
    if end < start:
        return None
    return start, end


class Auth(enum.Enum):
    PUBLIC = "public"                      # anyone who can reach the app
    WRITER = "writer"                      # a --upload-user login (everyone, when none is named)
    LOCAL_OR_WRITER = "local_or_writer"    # that, or a request from this computer itself

    def allows(self, *, writer: bool, local: bool) -> bool:
        if self is Auth.PUBLIC:
            return True
        if self is Auth.WRITER:
            return writer
        return writer or local
