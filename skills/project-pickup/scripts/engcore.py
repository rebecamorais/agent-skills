"""Shared, read-only building blocks for the project-pickup collector.

Nothing in this module can change GitHub or git state:

- GraphQL documents load only from ops/*.graphql and are rejected if they define a
  mutation or subscription. Only a VettedDocument can be POSTed, and only to /graphql.
- REST calls are GET only.
- git runs through subprocess with an argument list (no shell), GIT_OPTIONAL_LOCKS=0
  (so `git status` doesn't refresh the index) and a fixed set of read subcommands.
- Tokens are read from a file the user created. `gh` is never called, so nothing falls
  back to a broader keyring token.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

API_BASE = "https://api.github.com"
USER_AGENT = "eng-collect/1.0"
OPS_DIR = Path(__file__).resolve().parent / "ops"
SCHEMA_DIR = Path(__file__).resolve().parent / "schema"

REQUEST_TIMEOUT_S = 20
MAX_RETRIES = 2
MIN_RATE_REMAINING = 500
MAX_RETRY_AFTER_S = 30


# --------------------------------------------------------------------------- errors


class SourceError(Exception):
    """A data source failed. The message must never contain a token."""


class BudgetExceeded(SourceError):
    """The request budget or the deadline ran out."""


class UnsafeOperation(Exception):
    """Something tried to leave the read-only envelope. This is a bug, never retried."""


# --------------------------------------------------------------------------- paths


def state_dir() -> Path:
    if os.environ.get("ENG_STATE_DIR"):
        return Path(os.environ["ENG_STATE_DIR"]).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base).expanduser() / "eng"


def config_dir() -> Path:
    if os.environ.get("ENG_CONFIG_DIR"):
        return Path(os.environ["ENG_CONFIG_DIR"]).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / "eng"


def repo_slug(repo: str) -> str:
    """'owner/name' -> 'owner__name', safe as a directory name."""
    return repo.replace("/", "__")


def safe_name(value: str) -> str:
    """A branch name or similar as a single safe path component."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned[:120] or "unnamed"


def ensure_private_dir(path: Path) -> Path:
    """Create path and any missing parents as mode 700 (existing parents are left alone)."""
    missing = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    for folder in reversed(missing):
        folder.mkdir(mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)  # mkdir's mode is filtered by the umask
    return path


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write via temp file + os.replace so readers never see half a file."""
    ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- tokens


def read_token(path: Path) -> str:
    """Read a token file. Refuses group/world-readable files. Never echoes the token."""
    try:
        info = path.stat()
    except FileNotFoundError:
        raise SourceError(f"token file {path} not found") from None
    if info.st_mode & 0o077:
        raise SourceError(f"token file {path} is readable by other users; run: chmod 600 {path}")
    token = path.read_text(encoding="utf-8").strip()
    if not token or any(ch.isspace() for ch in token):
        raise SourceError(f"token file {path} is empty or malformed")
    return token


# --------------------------------------------------------------------------- GraphQL guard

_COMMENT = re.compile(r"#[^\n]*")
_BLOCK_STRING = re.compile(r'"""(?:\\"""|[^"]|"(?!""))*"""', re.S)
_STRING = re.compile(r'"(?:\\.|[^"\\\n])*"')
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[{}()]")


def operation_types(document: str) -> list[str]:
    """Return the operation type of each top-level definition.

    Strings and comments are removed first, so a field argument containing the word
    "mutation" doesn't count. Shorthand `{ ... }` is a query.
    """
    text = _BLOCK_STRING.sub('""', document)
    text = _STRING.sub('""', text)
    text = _COMMENT.sub("", text)
    depth = 0
    paren = 0
    types: list[str] = []
    expecting_definition = True
    for token in _TOKEN.findall(text):
        if token == "(":
            paren += 1
            continue
        if token == ")":
            paren -= 1
            continue
        if paren:
            continue
        if token == "{":
            if depth == 0 and expecting_definition:
                types.append("query")
            depth += 1
            expecting_definition = False
            continue
        if token == "}":
            depth -= 1
            if depth == 0:
                expecting_definition = True
            continue
        if depth == 0 and expecting_definition:
            types.append(token)
            expecting_definition = False
    return types


@dataclass(frozen=True)
class VettedDocument:
    name: str
    text: str


_vetted_registry: dict[str, VettedDocument] = {}


def load_graphql(name: str, ops_dir: Path = OPS_DIR) -> VettedDocument:
    """Load ops/<name>.graphql. Only query (and fragment) definitions are accepted."""
    if not re.fullmatch(r"[a-z0-9_]+", name):
        raise UnsafeOperation(f"invalid operation name {name!r}")
    text = (ops_dir / f"{name}.graphql").read_text(encoding="utf-8")
    types = operation_types(text)
    if not types or any(t not in ("query", "fragment") for t in types):
        raise UnsafeOperation(f"ops/{name}.graphql must contain only queries, found: {types}")
    if "query" not in types:
        raise UnsafeOperation(f"ops/{name}.graphql has no query")
    doc = VettedDocument(name=name, text=text)
    _vetted_registry[name] = doc
    return doc


def vet_all(ops_dir: Path = OPS_DIR) -> list[str]:
    """Load every shipped operation at startup; any bad file stops the collector."""
    names = sorted(p.stem for p in ops_dir.glob("*.graphql"))
    for name in names:
        load_graphql(name, ops_dir)
    return names


# --------------------------------------------------------------------------- HTTP

Transport = Callable[[urllib.request.Request, float], tuple[int, dict[str, str], bytes]]


def urllib_transport(request: urllib.request.Request, timeout: float) -> tuple[int, dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed host)
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in (error.headers or {}).items()}, error.read() or b""


@dataclass
class Budget:
    max_requests: int
    deadline_s: float
    started: float = field(default_factory=time.monotonic)
    requests: int = 0
    rate_remaining: int | None = None

    def remaining_s(self) -> float:
        return self.deadline_s - (time.monotonic() - self.started)

    def spend(self) -> float:
        """Account for one request and return the timeout to use for it."""
        if self.requests >= self.max_requests:
            raise BudgetExceeded(f"request budget of {self.max_requests} used up")
        left = self.remaining_s()
        if left <= 1:
            raise BudgetExceeded(f"time limit of {self.deadline_s:.0f}s reached")
        self.requests += 1
        return min(REQUEST_TIMEOUT_S, left)

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


class GitHub:
    """Minimal read-only GitHub client: GET for REST, vetted documents for GraphQL."""

    def __init__(
        self,
        token: str,
        budget: Budget,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        base: str = API_BASE,
    ) -> None:
        self._token = token
        self.budget = budget
        self._transport = transport
        self._sleep = sleep
        self._base = base.rstrip("/")

    # -- public API

    def graphql(self, doc: VettedDocument, variables: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(doc, VettedDocument) or _vetted_registry.get(doc.name) is not doc:
            raise UnsafeOperation("only documents loaded through load_graphql() may be sent")
        if self.budget.rate_remaining is not None and self.budget.rate_remaining < MIN_RATE_REMAINING:
            raise BudgetExceeded(f"GraphQL rate limit low ({self.budget.rate_remaining} points left)")
        body = json.dumps({"query": doc.text, "variables": variables}).encode()
        payload = self._request("POST", "/graphql", body=body)
        if payload.get("errors"):
            first = payload["errors"][0]
            kind = first.get("type", "ERROR")
            message = str(first.get("message", ""))[:200]
            if kind == "NOT_FOUND":
                raise SourceError(f"not found or no access: {message}")
            raise SourceError(f"GraphQL {kind}: {message}")
        data = payload.get("data") or {}
        rate = data.get("rateLimit") or {}
        if isinstance(rate.get("remaining"), int):
            self.budget.rate_remaining = rate["remaining"]
        return data

    def rest_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not path.startswith("/") or path.startswith("/graphql") or "//" in path or ".." in path:
            raise UnsafeOperation(f"invalid REST path {path!r}")
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        return self._request("GET", path + query)

    # -- internals

    def _request(self, method: str, path: str, body: bytes | None = None) -> Any:
        if method == "GET":
            if body is not None:
                raise UnsafeOperation("GET requests carry no body")
        elif method == "POST":
            if path != "/graphql":
                raise UnsafeOperation("POST is only allowed to /graphql")
        else:
            raise UnsafeOperation(f"HTTP method {method} is not allowed")

        url = self._base + path
        attempt = 0
        while True:
            timeout = self.budget.spend()
            request = urllib.request.Request(url, data=body, method=method)
            request.add_header("Authorization", f"Bearer {self._token}")
            request.add_header("Accept", "application/vnd.github+json")
            request.add_header("X-GitHub-Api-Version", "2022-11-28")
            request.add_header("User-Agent", USER_AGENT)
            if body is not None:
                request.add_header("Content-Type", "application/json")
            try:
                status, headers, raw = self._transport(request, timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                reason = getattr(error, "reason", error)
                if attempt < MAX_RETRIES:
                    attempt += 1
                    self._backoff(attempt, None)
                    continue
                raise SourceError(f"network error: {self._scrub(str(reason))}") from None

            if 200 <= status < 300:
                try:
                    return json.loads(raw.decode("utf-8") or "null")
                except ValueError:
                    raise SourceError(f"{method} {path.split('?')[0]}: response is not JSON") from None

            text = raw.decode("utf-8", "replace")[:300]
            secondary = status in (403, 429) and (
                "retry-after" in headers or "secondary rate limit" in text.lower()
            )
            if (status >= 500 or secondary) and attempt < MAX_RETRIES:
                attempt += 1
                self._backoff(attempt, headers.get("retry-after"))
                continue
            hint = {
                401: "token rejected (expired or revoked?)",
                403: "forbidden: the token lacks a permission for this data, or rate limited",
                404: "not found, or the token can't see it",
            }.get(status, "request failed")
            raise SourceError(f"HTTP {status} on {path.split('?')[0]}: {hint}")

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after and retry_after.isdigit():
            delay = min(int(retry_after), MAX_RETRY_AFTER_S)
        else:
            delay = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
        if delay >= self.budget.remaining_s() - 1:
            raise BudgetExceeded("no time left to retry within the time limit")
        self._sleep(delay)

    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text


def paginate(
    fetch_page: Callable[[str | None], tuple[list[Any], dict[str, Any], int | None]],
    max_pages: int = 10,
) -> tuple[list[Any], bool, int | None]:
    """Follow GraphQL cursors. Returns (nodes, truncated, total_count)."""
    nodes: list[Any] = []
    cursor = None
    total = None
    for _ in range(max_pages):
        page, info, total = fetch_page(cursor)
        nodes.extend(page)
        if not info.get("hasNextPage"):
            return nodes, False, total
        cursor = info.get("endCursor")
    return nodes, True, total


# --------------------------------------------------------------------------- git

GIT_READ_SUBCOMMANDS = {"worktree", "status", "log", "rev-list", "rev-parse", "remote", "diff", "check-ignore"}
GIT_TIMEOUT_S = 10


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    if not args or args[0] not in GIT_READ_SUBCOMMANDS:
        raise UnsafeOperation(f"git subcommand {args[:1]} is not in the read-only list")
    if args[0] == "worktree" and args[1:2] != ["list"]:
        raise UnsafeOperation("only `git worktree list` is allowed")
    if args[0] == "remote" and args[1:2] != ["get-url"]:
        raise UnsafeOperation("only `git remote get-url` is allowed")
    # No optional locks (status won't refresh the index), no prompts, no fsmonitor hook.
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    command = ["git", "-c", "core.fsmonitor=false", "-C", str(cwd), *args]
    return subprocess.run(command, capture_output=True, text=True, timeout=GIT_TIMEOUT_S, env=env, check=False)


def git(args: list[str], cwd: Path) -> str:
    """Run a read-only git subcommand. Returns stdout; raises SourceError on failure."""
    try:
        result = _run_git(args, cwd)
    except subprocess.TimeoutExpired:
        raise SourceError(f"git {args[0]} timed out in {cwd}") from None
    except FileNotFoundError:
        raise SourceError("git is not installed") from None
    if result.returncode != 0:
        raise SourceError(f"git {args[0]} failed in {cwd}: {result.stderr.strip()[:200]}")
    return result.stdout


def git_ok(args: list[str], cwd: Path) -> int:
    """Run a read-only git subcommand and return only its exit code (e.g. check-ignore)."""
    try:
        return _run_git(args, cwd).returncode
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 128


_REMOTE = re.compile(r"^(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def repo_identity(path: Path) -> tuple[Path, str, bool]:
    """Return (main worktree root, repo id, is_github) for the repository containing path.

    A github.com 'origin' gives 'owner/name'. Without one (no remote, or another host) the
    repository still works in local-only mode, as 'local/<folder>-<hash of its path>'.
    Raises SourceError if path is not inside a git repository.
    """
    common = Path(git(["rev-parse", "--path-format=absolute", "--git-common-dir"], path).strip())
    main_root = common.parent if common.name == ".git" else common
    try:
        repo = parse_github_remote(git(["remote", "get-url", "origin"], path))
    except SourceError:
        repo = None
    if repo:
        return main_root, repo, True
    digest = hashlib.sha256(str(main_root).encode()).hexdigest()[:6]
    return main_root, f"local/{safe_name(main_root.name)[:60]}-{digest}", False


def parse_github_remote(url: str) -> str | None:
    match = _REMOTE.match(url.strip())
    if not match or {match.group(1), match.group(2)} & {".", ".."}:
        return None
    return f"{match.group(1)}/{match.group(2)}"


# --------------------------------------------------------------------------- schema validation


def validate(instance: Any, schema: dict[str, Any], root: dict[str, Any] | None = None, path: str = "$") -> list[str]:
    """Validate against a small JSON Schema subset.

    Supports: $ref (#/$defs/...), type (string or list), enum, const, required, properties,
    additionalProperties (bool or schema), items, maxItems, maxLength, minimum, pattern.
    """
    root = root or schema
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/$defs/"):
            return [f"{path}: unsupported $ref {ref}"]
        return validate(instance, root["$defs"][ref.split("/")[-1]], root, path)

    errors: list[str] = []
    expected = schema.get("type")
    if expected:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_is_type(instance, t) for t in types):
            return [f"{path}: expected {'/'.join(types)}, got {type(instance).__name__}"]
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: must be {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in {schema['enum']}")
    if isinstance(instance, str):
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: does not match {schema['pattern']}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: below {schema['minimum']}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing {key}")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in props:
                errors.extend(validate(value, props[key], root, f"{path}.{key}"))
            elif extra is False:
                errors.append(f"{path}: unexpected property {key}")
            elif isinstance(extra, dict):
                errors.extend(validate(value, extra, root, f"{path}.{key}"))
    if isinstance(instance, list):
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for index, item in enumerate(instance):
                errors.extend(validate(item, schema["items"], root, f"{path}[{index}]"))
    return errors[:20]


def _is_type(value: Any, name: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(name, False)


def load_schema(name: str = "snapshot.v1.json") -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- wording

# Solo-maintainer principle: no pressure, no scoring. Checked by tests on every text the
# collector emits and on the skill's own instructions.
BANNED_WORDS = (
    "overdue", "stale", "behind schedule", "late", "velocity", "should have",
    "failed to", "lazy", "slow", "deadline", "productivity", "streak",
)


def banned_words_in(text: str) -> list[str]:
    lowered = text.lower()
    return [word for word in BANNED_WORDS if re.search(rf"\b{re.escape(word)}\b", lowered)]


def clip(text: str | None, limit: int = 100) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
