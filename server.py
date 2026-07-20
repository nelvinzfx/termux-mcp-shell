#!/usr/bin/env python3
"""MCP Streamable HTTP server: full Termux shell access plus file tools."""
import asyncio
import base64
import difflib
import functools
import hashlib
import hmac
import json
import os
import pathlib
import re
import signal
import tempfile
import threading
import unicodedata
import uuid
from collections import OrderedDict
from typing import Literal, TypedDict

from mcp.server.fastmcp import FastMCP
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

READ_MAX_LINES = int(os.environ.get("MCP_READ_MAX_LINES", "2000"))
READ_MAX_BYTES = int(os.environ.get("MCP_READ_MAX_BYTES", str(50 * 1024)))

HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8088"))
TRUNC_LIMIT = int(os.environ.get("MCP_TRUNC_LIMIT", "8192"))
MAX_SESSIONS = int(os.environ.get("MCP_MAX_SESSIONS", "50"))
TEMP_ROOT = pathlib.Path(os.environ.get("TMPDIR") or tempfile.gettempdir()).resolve()
HOME_ROOT = pathlib.Path(os.environ.get("HOME") or pathlib.Path.home()).expanduser().resolve()


# session_id -> {"stdout": bytes, "stderr": bytes}
_buffers: "OrderedDict[str, dict]" = OrderedDict()

# Serialises validation+publication so concurrent stale commits cannot race.
_TRANSACTION_LOCK = threading.RLock()

mcp = FastMCP("termux-shell", host=HOST, port=PORT)


def _tool_path(path: str) -> pathlib.Path:
    """Resolve file paths against HOME and map Android's missing /tmp."""
    if path == "/tmp" or path.startswith("/tmp/"):
        root = TEMP_ROOT.resolve(strict=False)
        relative = pathlib.PurePosixPath(path).relative_to("/tmp")
        mapped = root.joinpath(*relative.parts).resolve(strict=False)
        if mapped != root and root not in mapped.parents:
            raise ValueError("/tmp path escapes the temporary directory")
        return mapped
    expanded = pathlib.Path(path).expanduser()
    return expanded if expanded.is_absolute() else HOME_ROOT / expanded


def _threaded_tool(fn):
    """Register blocking filesystem work off the ASGI event loop."""
    @functools.wraps(fn)
    async def call(*args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)
    mcp.tool()(call)
    return fn


def _serialized_threaded_tool(fn):
    """Offload a mutating tool while preserving write transaction ordering."""
    @functools.wraps(fn)
    def call(*args, **kwargs):
        with _TRANSACTION_LOCK:
            return fn(*args, **kwargs)
    return _threaded_tool(call)


class AuthMiddleware:
    """Pure-ASGI middleware: optional Bearer / X-API-Key token auth.

    Active only when MCP_AUTH_TOKEN env is set. When unset, no auth is applied
    and the server behaves exactly as before (open access on the bind address).
    """

    def __init__(self, app, token: str):
        self.app = app
        self._token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode(errors="replace")
        api_key = headers.get(b"x-api-key", b"").decode(errors="replace")
        provided = ""
        if auth.startswith("Bearer "):
            provided = auth[7:].strip()
        elif api_key:
            provided = api_key.strip()
        if not hmac.compare_digest(provided.encode(), self._token):
            if scope["type"] == "http":
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                })
                await send({"type": "http.response.body",
                            "body": json.dumps({"error": "unauthorized"}).encode()})
            return
        await self.app(scope, receive, send)


def _store(stdout: bytes, stderr: bytes) -> str:
    sid = uuid.uuid4().hex[:12]
    _buffers[sid] = {"stdout": stdout, "stderr": stderr}
    while len(_buffers) > MAX_SESSIONS:  # FIFO evict
        _buffers.popitem(last=False)
    return sid


def _head(data: bytes) -> tuple[str, bool, int]:
    total = len(data)
    chunk = data[:TRUNC_LIMIT]
    return chunk.decode(errors="replace"), total > TRUNC_LIMIT, total


class RunResult(TypedDict):
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    stdout_total_bytes: int
    stderr_total_bytes: int
    session_id: str | None
    error: str | None
    stdout_next_offset: int | None
    stderr_next_offset: int | None


class ReadResult(TypedDict):
    data: str | None
    offset: int
    length: int
    total_bytes: int
    eof: bool
    error: str | None


_CANONICAL_MODES = ("replace_match", "insert_before", "insert_after")
_MODE_ALIASES = {
    "insert_before_match": "insert_before",
    "insert_after_match": "insert_after",
}
_LEGACY_EDIT_KEYS = {"old_text", "new_text", "oldText", "newText"}
_EDIT_CONTRACT = (
    "required fields: mode, match_text (or matchText), and write_text (or writeText); "
    "allowed canonical modes: replace_match, insert_before, insert_after; accepted mode "
    "aliases: insert_before_match, insert_after_match. old_text/new_text and "
    "oldText/newText are unsupported"
)


class EditSpec(BaseModel):
    """One canonical text edit. Camel-case field aliases are accepted as input."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["replace_match", "insert_before", "insert_after"] = Field(
        description=("Canonical mode. Compatibility input also accepts "
                     "insert_before_match and insert_after_match."))
    match_text: str = Field(
        min_length=1,
        validation_alias=AliasChoices("match_text", "matchText"),
        description="Unique non-empty source or anchor; matchText is accepted as input.")
    write_text: str = Field(
        validation_alias=AliasChoices("write_text", "writeText"),
        description="Literal replacement/insertion; writeText is accepted as input.")

    @model_validator(mode="before")
    @classmethod
    def normalize_compatibility_input(cls, value):
        if not isinstance(value, dict):
            return value
        legacy = sorted(_LEGACY_EDIT_KEYS.intersection(value))
        if legacy:
            raise ValueError(f"unsupported fields {legacy}: {_EDIT_CONTRACT}")
        data = dict(value)
        for canonical, alias in (("match_text", "matchText"),
                                 ("write_text", "writeText")):
            if canonical in data and alias in data and data[canonical] != data[alias]:
                raise ValueError(
                    f"conflicting values for {canonical} and {alias}; {_EDIT_CONTRACT}")
            if canonical not in data and alias in data:
                data[canonical] = data[alias]
            data.pop(alias, None)
        missing = [
            name for name, alias in (("mode", None), ("match_text", "matchText"),
                                     ("write_text", "writeText"))
            if name not in data and (alias is None or alias not in data)
        ]
        if missing:
            raise ValueError(f"missing {', '.join(missing)}; {_EDIT_CONTRACT}")
        mode = data.get("mode")
        if mode in _MODE_ALIASES:
            data["mode"] = _MODE_ALIASES[mode]
        elif mode not in _CANONICAL_MODES:
            raise ValueError(f"invalid mode {mode!r}; {_EDIT_CONTRACT}")
        return data


class EditFileSpec(BaseModel):
    """One existing UTF-8 file in an edit_files transaction."""

    model_config = ConfigDict(extra="ignore")

    path: str
    edits: list[EditSpec] = Field(min_length=1)
    expected_sha256: str | None = None

    @model_validator(mode="before")
    @classmethod
    def require_edits(cls, value):
        if isinstance(value, dict) and "edits" not in value:
            if "content" in value:
                raise ValueError(
                    "edit_files edits existing UTF-8 files and requires a non-empty edits "
                    "array; use write_file to create or replace a file")
            raise ValueError(
                "edit_files requires each item to contain path and a non-empty edits array")
        return value


class ReadFileSpec(BaseModel):
    """One range in a read_files batch."""

    model_config = ConfigDict(extra="ignore")

    path: str
    offset: int = 1
    limit: int | None = None
    line_numbers: bool = True


def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the command's isolated process group, including descendants."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        if proc.returncode is None:
            proc.kill()


@mcp.tool()
async def run_command(command: str, timeout: float | None = None,
                      cwd: str | None = None) -> RunResult:
    """Run a shell command via /bin/sh -c and return stdout/stderr/exit_code.

    Long output is truncated to MCP_TRUNC_LIMIT bytes; full output is kept in a
    buffer readable via read_output using the returned session_id. Omitted cwd
    defaults to HOME; relative cwd resolves from HOME. A cwd under /tmp maps to
    Termux's TMPDIR. Command text is never rewritten: use $TMPDIR/... or a resolved
    path returned by a file tool when crossing from file tools into shell commands.
    """
    try:
        resolved_cwd = str(_tool_path(cwd)) if cwd is not None else str(HOME_ROOT)
        spawn_task = asyncio.create_task(asyncio.create_subprocess_shell(
            command, cwd=resolved_cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            executable="/bin/sh", start_new_session=True,
        ))
    except Exception as error:
        return {"error": f"spawn failed: {error}", "exit_code": None, "timed_out": False,
                "stdout": "", "stderr": "", "stdout_truncated": False,
                "stderr_truncated": False, "stdout_total_bytes": 0,
                "stderr_total_bytes": 0, "session_id": None,
                "stdout_next_offset": None, "stderr_next_offset": None}
    try:
        proc = await asyncio.shield(spawn_task)
    except asyncio.CancelledError as cancelled:
        try:
            proc = await asyncio.shield(spawn_task)
        except Exception:
            raise cancelled
        _kill_process_group(proc)
        await asyncio.shield(proc.communicate())
        raise
    except Exception as e:
        return {"error": f"spawn failed: {e}", "exit_code": None, "timed_out": False,
                "stdout": "", "stderr": "", "stdout_truncated": False, "stderr_truncated": False,
                "stdout_total_bytes": 0, "stderr_total_bytes": 0, "session_id": None,
                "stdout_next_offset": None, "stderr_next_offset": None}

    timed_out = False
    communicate_task = asyncio.create_task(proc.communicate())
    try:
        done, _ = await asyncio.wait({communicate_task}, timeout=timeout)
        if communicate_task in done:
            out, err = communicate_task.result()
        else:
            timed_out = True
            _kill_process_group(proc)
            out, err = await asyncio.shield(communicate_task)
    except asyncio.CancelledError:
        _kill_process_group(proc)
        try:
            await asyncio.shield(communicate_task)
        finally:
            raise

    out_s, out_trunc, out_total = _head(out)
    err_s, err_trunc, err_total = _head(err)

    sid = _store(out, err) if (out_trunc or err_trunc) else None

    return {
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "stdout": out_s,
        "stderr": err_s,
        "stdout_truncated": out_trunc,
        "stderr_truncated": err_trunc,
        "stdout_total_bytes": out_total,
        "stderr_total_bytes": err_total,
        "session_id": sid,
        "stdout_next_offset": TRUNC_LIMIT if out_trunc else None,
        "stderr_next_offset": TRUNC_LIMIT if err_trunc else None,
        "error": None,
    }


@mcp.tool()
def read_output(session_id: str, stream: str = "stdout", offset: int = 0, length: int = 4096) -> ReadResult:
    """Read a byte range from a buffered output stream (paginate large output)."""
    err_base = {"data": None, "offset": offset, "length": 0, "total_bytes": 0, "eof": True}
    buf = _buffers.get(session_id)
    if buf is None:
        return {**err_base, "error": "session not found"}
    if stream not in ("stdout", "stderr"):
        return {**err_base, "error": "stream must be 'stdout' or 'stderr'"}
    data = buf[stream]
    chunk = data[offset:offset + length]
    return {
        "data": chunk.decode(errors="replace"),
        "offset": offset,
        "length": len(chunk),
        "total_bytes": len(data),
        "eof": offset + len(chunk) >= len(data),
        "error": None,
    }


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------

def _normalize_lf(t: str) -> str:
    return t.replace("\r\n", "\n").replace("\r", "\n")


def _detect_ending(t: str) -> str:
    crlf = t.find("\r\n")
    lf = t.find("\n")
    if lf == -1 or crlf == -1:
        return "\n"
    return "\r\n" if crlf < lf else "\n"


def _restore_ending(t: str, ending: str) -> str:
    return t.replace("\n", "\r\n") if ending == "\r\n" else t


def _strip_bom(t: str) -> tuple[str, str]:
    return ("\ufeff", t[1:]) if t.startswith("\ufeff") else ("", t)


_SMART_SINGLE = re.compile(r"[\u2018\u2019\u201A\u201B]")
_SMART_DOUBLE = re.compile(r"[\u201C\u201D\u201E\u201F]")
_DASHES = re.compile(r"[\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")
_SPACES = re.compile(r"[\u00A0\u2002-\u200A\u202F\u205F\u3000]")


def _fuzzy_projection(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Return fuzzy comparison text plus source spans for every output char."""
    chars: list[str] = []
    kept_spans: list[tuple[int, int]] = []
    cursor = 0
    while True:
        newline = text.find("\n", cursor)
        line_end = len(text) if newline == -1 else newline
        source_line = text[cursor:line_end]
        normalized = unicodedata.normalize("NFKC", source_line)

        spans: list[tuple[int, int]] = []
        if normalized == source_line:
            spans = [(cursor + i, cursor + i + 1) for i in range(len(normalized))]
        else:
            matcher = difflib.SequenceMatcher(
                None, source_line, normalized, autojunk=False)
            source_pos = normalized_pos = 0
            for block in matcher.get_matching_blocks():
                if block.b > normalized_pos:
                    span = (cursor + source_pos, cursor + block.a)
                    spans.extend(span for _ in range(block.b - normalized_pos))
                spans.extend(
                    (cursor + block.a + i, cursor + block.a + i + 1)
                    for i in range(block.size))
                source_pos = block.a + block.size
                normalized_pos = block.b + block.size

        trimmed_end = len(normalized.rstrip())
        chars.extend(normalized[:trimmed_end])
        kept_spans.extend(spans[:trimmed_end])
        if newline == -1:
            break
        chars.append("\n")
        kept_spans.append((newline, newline + 1))
        cursor = newline + 1

    for i, char in enumerate(chars):
        if _SMART_SINGLE.fullmatch(char):
            chars[i] = "'"
        elif _SMART_DOUBLE.fullmatch(char):
            chars[i] = '"'
        elif _DASHES.fullmatch(char):
            chars[i] = "-"
        elif _SPACES.fullmatch(char):
            chars[i] = " "
    return "".join(chars), kept_spans


def _fuzzy(text: str) -> str:
    return _fuzzy_projection(text)[0]


def _all_occurrences(content: str, search: str) -> list[int]:
    if not search:
        return []
    starts = []
    cursor = 0
    while True:
        index = content.find(search, cursor)
        if index == -1:
            return starts
        starts.append(index)
        cursor = index + 1


def _find_matches(content: str, search: str) -> tuple[list[tuple[int, int]], bool]:
    exact = _all_occurrences(content, search)
    if exact:
        return [(start, start + len(search)) for start in exact], False

    projected, spans = _fuzzy_projection(content)
    target = _fuzzy(search)
    fuzzy_starts = _all_occurrences(projected, target)
    matches = []
    for start in fuzzy_starts:
        selected = spans[start:start + len(target)]
        if not selected:
            continue
        source_start = min(span[0] for span in selected)
        source_end = max(span[1] for span in selected)
        match = (source_start, source_end)
        source_slice = content[source_start:source_end]
        if (source_end > source_start and _fuzzy(source_slice) == target
                and match not in matches):
            matches.append(match)
    return matches, True


def _find_indent(content: str, old: str) -> tuple[bool, int, int, str]:
    """Indent-insensitive line match. Returns (found, char_index, match_len, matched_text)."""
    c_lines = content.split("\n")
    o_stripped = [l.strip() for l in old.split("\n")]
    n = len(o_stripped)
    hits = [s for s in range(len(c_lines) - n + 1)
            if all(c_lines[s + j].strip() == o_stripped[j] for j in range(n))]
    if len(hits) != 1:
        return False, -1, 0, ""
    s = hits[0]
    char_idx = sum(len(c_lines[k]) + 1 for k in range(s))
    matched = "\n".join(c_lines[s:s + n])
    return True, char_idx, len(matched), matched


def _reindent(new: str, matched: str, old: str) -> str:
    """Re-indent write_text by the indent delta between the matched block and what the model sent.

    Applies the delta to each line individually based on that line's own indent,
    so multi-level indentation (e.g. 8sp + 12sp + 16sp) is preserved correctly.
    """
    def indent_len(line: str) -> int:
        return len(line) - len(line.lstrip())
    actual = indent_len(matched.split("\n")[0])
    intended = indent_len(old.split("\n")[0])
    if actual == intended:
        return new
    delta = actual - intended
    if delta == 0:
        return new
    out = []
    for line in new.split("\n"):
        stripped = line.lstrip()
        if not stripped:  # blank or whitespace-only line — preserve as empty
            out.append("")
            continue
        current = indent_len(line)
        new_indent = max(0, current + delta)
        out.append(" " * new_indent + stripped)
    return "\n".join(out)


def _match_one(base: str, old: str, new: str, path: str, label: str):
    """Resolve one edit in original source coordinates."""
    matches, _ = _find_matches(base, old)
    if len(matches) > 1:
        raise ValueError(f"Found {len(matches)} occurrences of {label} in {path}. Each match_text must "
                         f"be unique. Provide more context.")
    if matches:
        start, end = matches[0]
        return start, end - start, new
    fi, fidx, flen, matched = _find_indent(base, old)
    if fi:
        return fidx, flen, _reindent(new, matched, old)
    o_stripped = [l.strip() for l in old.split("\n")]
    c_lines = base.split("\n")
    nl = len(o_stripped)
    occ = sum(1 for s in range(len(c_lines) - nl + 1)
              if all(c_lines[s + j].strip() == o_stripped[j] for j in range(nl)))
    if occ > 1:
        raise ValueError(f"Found {occ} indent-insensitive matches of {label} in {path}. "
                         f"Provide more surrounding context to make it unique.")
    raise ValueError(f"Could not find {label} in {path}. The line content must match "
                     f"(leading/trailing whitespace differences are tolerated).")


# ---------------------------------------------------------------------------
# Edit normalisation and resolution
# ---------------------------------------------------------------------------

def _normalize_edit(e: dict | EditSpec) -> dict:
    """Validate one explicit edit spec, accept aliases, and normalize output."""
    if isinstance(e, BaseModel):
        e = e.model_dump()
    if not isinstance(e, dict):
        raise ValueError(f"edit must be an object; {_EDIT_CONTRACT}")
    legacy = sorted(_LEGACY_EDIT_KEYS.intersection(e))
    if legacy:
        raise ValueError(f"unsupported fields {legacy}: {_EDIT_CONTRACT}")
    supported = {"mode", "match_text", "matchText", "write_text", "writeText"}
    unknown = sorted(set(e).difference(supported))
    if unknown:
        raise ValueError(f"unsupported fields {unknown}: {_EDIT_CONTRACT}")

    def aliased(canonical: str, alias: str):
        if canonical in e and alias in e and e[canonical] != e[alias]:
            raise ValueError(
                f"conflicting values for {canonical} and {alias}; {_EDIT_CONTRACT}")
        if canonical in e:
            return e[canonical]
        if alias in e:
            return e[alias]
        raise ValueError(f"missing {canonical}; {_EDIT_CONTRACT}")

    mode = e.get("mode")
    if mode in _MODE_ALIASES:
        mode = _MODE_ALIASES[mode]
    elif mode not in _CANONICAL_MODES:
        raise ValueError(f"invalid mode {mode!r}; {_EDIT_CONTRACT}")
    match_text = aliased("match_text", "matchText")
    write_text = aliased("write_text", "writeText")
    if not isinstance(match_text, str) or not isinstance(write_text, str):
        raise ValueError(f"match and write values must be strings; {_EDIT_CONTRACT}")
    if not match_text:
        raise ValueError(f"match_text/matchText must be non-empty; {_EDIT_CONTRACT}")
    return {
        "mode": mode,
        "match_text": _normalize_lf(match_text),
        "write_text": _normalize_lf(write_text),
    }


def _resolve_insert(base: str, mode: str, anchor: str,
                    content: str, path: str, label: str) -> tuple[int, int, str]:
    """Resolve an insert edit. Returns (insert_index, 0, content).

    Anchor must be unique. Content is literal; no newline is added. When the
    anchor is matched indent-insensitively, content is reindented to the
    anchor's actual indentation.
    """
    reindented = content
    matches, _ = _find_matches(base, anchor)
    if len(matches) > 1:
        raise ValueError(f"{label}: anchor found {len(matches)} times in {path}. Anchor must be unique.")
    if matches:
        idx, end = matches[0]
        mlen = end - idx
    else:
        fi, fidx, flen, matched = _find_indent(base, anchor)
        if fi:
            idx = fidx
            mlen = flen
            reindented = _reindent(content, matched, anchor)
        else:
            o_stripped = [l.strip() for l in anchor.split("\n")]
            c_lines = base.split("\n")
            nl = len(o_stripped)
            occ = sum(1 for s in range(len(c_lines) - nl + 1)
                      if all(c_lines[s + j].strip() == o_stripped[j] for j in range(nl)))
            if occ > 1:
                raise ValueError(f"{label}: anchor found {occ} times (indent-insensitive) in {path}. "
                                 f"Anchor must be unique.")
            raise ValueError(f"{label}: anchor not found in {path}.")

    if mode == "insert_before":
        return idx, 0, reindented
    return idx + mlen, 0, reindented


# ---------------------------------------------------------------------------
# Diagnostics helpers (bounded, never include large unrelated content)
# ---------------------------------------------------------------------------

def _diagnostic_lines(content: str) -> list[str]:
    """Return LF-delimited source lines without synthesizing lines from bare CRs."""
    lines = content.split("\n")
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _closest_match(content: str, search: str, max_excerpt: int = 500) -> dict:
    """Find the closest matching real source line for no-match diagnostics."""
    search_first = search.strip().split("\n")[0] if search.strip() else search
    if not search_first:
        return {}
    content_lines = _diagnostic_lines(content)
    best_ratio = 0.0
    best_line = -1
    for i, line in enumerate(content_lines):
        ratio = difflib.SequenceMatcher(None, search_first, line.strip()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_line = i
    if best_line < 0 or best_ratio < 0.2:
        return {}
    start = max(0, best_line - 2)
    end = min(len(content_lines), best_line + 3)
    lines = []
    for j in range(start, end):
        prefix = ">>" if j == best_line else "  "
        source_line = content_lines[j].replace("\r", "\\r")
        lines.append(f"{prefix} {j + 1}: {source_line}")
    return {
        "closest_match_line": best_line + 1,
        "similarity": round(best_ratio, 3),
        "nearby_text": "\n".join(lines)[:max_excerpt],
    }


def _ambiguity_info(content: str, search: str, max_candidates: int = 5) -> dict:
    """Return bounded candidate lines from the real source text."""
    content_lines = _diagnostic_lines(content)
    search_first = search.strip().split("\n")[0] if search.strip() else search
    if not search_first:
        return {}
    candidates = []
    for i, line in enumerate(content_lines):
        if search_first in line:
            text = line.strip().replace("\r", "\\r")
            candidates.append({"line": i + 1, "text": text[:200]})
        if len(candidates) >= max_candidates:
            break
    return {"candidate_lines": candidates} if candidates else {}


def _apply_edits(normalized: str, edits: list[dict], path: str,
                 diagnostic_content: str | None = None
                 ) -> tuple[str, str, list[dict], str | None]:
    """Resolve every edit against normalized text and diagnose from real source text."""
    norm = [_normalize_edit(e) for e in edits]
    original = normalized
    diagnostic_source = normalized if diagnostic_content is None else diagnostic_content
    base = normalized
    matched, results = [], []
    for i, edit in enumerate(norm):
        result = {
            "index": i,
            "mode": edit["mode"],
            "matched": False,
            "ok": False,
            "status": "failed",
            "reason": None,
            "error": None,
            "match_count": None,
        }
        label = f"edits[{i}]" if len(norm) > 1 else "the text"
        try:
            if edit["mode"] == "replace_match":
                idx, match_len, new_text = _match_one(
                    base, edit["match_text"], edit["write_text"], path, label)
            else:
                idx, match_len, new_text = _resolve_insert(
                    base, edit["mode"], edit["match_text"], edit["write_text"], path, label)
            result["match_count"] = 1
            matched.append({"i": i, "idx": idx, "len": match_len, "new": new_text})
            result.update(matched=True, ok=True, status="matched")
        except ValueError as error:
            result["reason"] = str(error)
            result["error"] = str(error)
            occurrences = len(_find_matches(original, edit["match_text"])[0])
            result["match_count"] = occurrences
            if occurrences == 0:
                result.update(_closest_match(diagnostic_source, edit["match_text"]))
            elif occurrences > 1:
                result.update(_ambiguity_info(diagnostic_source, edit["match_text"]))
        results.append(result)

    failure = next((result for result in results if not result["ok"]), None)
    ordered = sorted(matched, key=lambda item: (item["idx"], item["len"] == 0, item["i"]))
    for left, right in zip(ordered, ordered[1:]):
        left_end = left["idx"] + left["len"]
        overlaps = left_end > right["idx"]
        same_boundary = left["idx"] == right["idx"]
        if overlaps or same_boundary:
            message = f"edits[{left['i']}] and edits[{right['i']}] conflict in {path}. Merge them."
            result = results[right["i"]]
            result.update(matched=False, ok=False, status="failed", reason=message, error=message)
            failure = failure or result

    if failure:
        for result in results:
            if result["ok"]:
                result["status"] = "aborted"
        reason = failure.get("reason") or "edit failed"
        return base, base, results, f"atomic batch aborted: {reason}"

    new = base
    for match in sorted(matched, key=lambda item: item["idx"], reverse=True):
        new = new[:match["idx"]] + match["new"] + new[match["idx"] + match["len"]:]
    for result in results:
        result["status"] = "applied" if new != base else "matched_no_change"
    return base, new, results, None


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------

class WriteResult(TypedDict):
    ok: bool
    path: str
    bytes_written: int
    sha256: str | None
    error: str | None


def _atomic_write(p: pathlib.Path, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{p.name}.", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(temp, p)
    except Exception:
        try: os.unlink(temp)
        except OSError: pass
        raise


@_serialized_threaded_tool
def write_file(path: str, content: str, expected_sha256: str | None = None,
               create_only: bool = False) -> WriteResult:
    """Atomically create or replace one UTF-8 file, creating parent directories.

    Relative paths resolve from HOME; /tmp maps to Termux TMPDIR. With
    expected_sha256, the existing file must match that hash (case-insensitive), and
    a missing target is stale. With create_only=true, an existing target is rejected.
    Guards and publication run under the serialized transaction lock.
    """
    try:
        p = _tool_path(path)
        if expected_sha256 is not None and not isinstance(expected_sha256, str):
            raise ValueError("expected_sha256 must be a string or null")
        if not isinstance(create_only, bool):
            raise ValueError("create_only must be boolean")
        if create_only and expected_sha256 is not None:
            raise ValueError("create_only and expected_sha256 cannot be combined")
        exists = p.exists()
        path_entry_exists = exists or p.is_symlink()
        if create_only and path_entry_exists:
            current_sha = None
            if exists and p.is_file():
                current_sha = hashlib.sha256(p.read_bytes()).hexdigest()
            return {"ok": False, "path": str(p), "bytes_written": 0,
                    "sha256": current_sha,
                    "error": "create_only target already exists"}
        if expected_sha256 is not None:
            if not exists:
                return {"ok": False, "path": str(p), "bytes_written": 0,
                        "sha256": None,
                        "error": ("stale source: expected_sha256 supplied but target "
                                  "does not exist")}
            current_sha = hashlib.sha256(p.read_bytes()).hexdigest()
            if not hmac.compare_digest(expected_sha256.lower(), current_sha):
                return {"ok": False, "path": str(p), "bytes_written": 0,
                        "sha256": current_sha,
                        "error": (f"stale source: expected sha256 {expected_sha256}, "
                                  f"got {current_sha}")}
        data = content.encode("utf-8")
        _atomic_write(p, data)
        return {
            "ok": True,
            "path": str(p),
            "bytes_written": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "error": None,
        }
    except Exception as error:
        return {"ok": False, "path": path, "bytes_written": 0,
                "sha256": None, "error": str(error)}


@_serialized_threaded_tool
def append_file(path: str, content: str,
                expected_sha256: str | None = None) -> WriteResult:
    """Atomically append UTF-8 content; /tmp maps to Termux TMPDIR."""
    try:
        p = _tool_path(path)
        current = p.read_bytes() if p.exists() else b""
        current_sha = hashlib.sha256(current).hexdigest()
        if expected_sha256 is not None and not hmac.compare_digest(
                expected_sha256.lower(), current_sha):
            return {
                "ok": False,
                "path": str(p),
                "bytes_written": 0,
                "sha256": current_sha,
                "error": f"stale source: expected sha256 {expected_sha256}, got {current_sha}",
            }
        addition = content.encode("utf-8")
        data = current + addition
        _atomic_write(p, data)
        return {
            "ok": True,
            "path": str(p),
            "bytes_written": len(addition),
            "sha256": hashlib.sha256(data).hexdigest(),
            "error": None,
        }
    except Exception as error:
        return {"ok": False, "path": path, "bytes_written": 0,
                "sha256": None, "error": str(error)}


# ---------------------------------------------------------------------------
# Shared edit result schema
# ---------------------------------------------------------------------------

class EditResult(TypedDict):
    ok: bool
    applied: bool
    dry_run: bool
    path: str
    replacements: int
    changed: bool
    diff: str | None
    results: list[dict] | None
    batch_aborted: bool
    error: str | None


class ReadResult2(TypedDict):
    path: str
    content: str
    start_line: int
    end_line: int
    total_lines: int
    truncated: bool
    next_offset: int | None
    sha256: str | None
    error: str | None


@_threaded_tool
def read_file_bytes(path: str, offset: int = 0, length: int = 4096) -> dict:
    """Read bytes as base64; /tmp maps to Termux TMPDIR."""
    if offset < 0 or length < 0:
        return {"ok": False, "path": path, "offset": offset, "length": 0, "total_bytes": 0, "eof": True, "data_base64": "", "error": "offset and length must be non-negative"}
    try:
        p = _tool_path(path); total = p.stat().st_size
        with p.open("rb") as f: f.seek(offset); data = f.read(length)
        return {"ok": True, "path": str(p), "offset": offset, "length": len(data), "total_bytes": total, "eof": offset + len(data) >= total, "data_base64": base64.b64encode(data).decode("ascii"), "error": None}
    except Exception as e:
        return {"ok": False, "path": path, "offset": offset, "length": 0, "total_bytes": 0, "eof": True, "data_base64": "", "error": str(e)}


def _read_file(path: str, offset: int = 1, limit: int | None = None,
               line_numbers: bool = True) -> ReadResult2:
    try:
        p = _tool_path(path)
        raw_bytes = p.read_bytes()
        sha256 = hashlib.sha256(raw_bytes).hexdigest()
        text = raw_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        reported_path = str(p) if "p" in locals() else path
        return {"path": reported_path, "content": "", "start_line": offset,
                "end_line": 0, "total_lines": 0, "truncated": False,
                "next_offset": None, "sha256": None, "error": str(e)}
    lines = text.split("\n")
    final_newline = bool(lines and lines[-1] == "")
    if final_newline:
        lines.pop()
    total = len(lines)
    start = max(0, offset - 1)
    if start >= total:
        return {"path": str(p), "content": "", "start_line": offset,
                "end_line": 0, "total_lines": total, "truncated": False,
                "next_offset": None, "sha256": sha256,
                "error": f"offset {offset} is beyond end of file ({total} lines total)"}
    end = min(start + limit, total) if limit is not None else total
    end = min(end, start + READ_MAX_LINES)
    out, size, n = [], 0, 0
    for idx in range(start, end):
        if line_numbers:
            row = f"{idx + 1:6d}  {lines[idx]}"
            row_size = len(row.encode()) + 1
        else:
            row = lines[idx] + ("\n" if idx < total - 1 or final_newline else "")
            row_size = len(row.encode())
        size += row_size
        if n > 0 and size > READ_MAX_BYTES:
            end = start + n
            break
        out.append(row)
        n += 1
    truncated = end < total
    next_off = end + 1 if truncated else None
    body = "\n".join(out) if line_numbers else "".join(out)
    if truncated and line_numbers:
        remaining = total - (start + n)
        body += (f"\n\n--- TRUNCATED: showing lines {start + 1}-{start + n} of {total} "
                 f"({remaining} more). Use offset={next_off} to continue. ---")
    return {"path": str(p), "content": body, "start_line": start + 1,
            "end_line": start + n, "total_lines": total, "truncated": truncated,
            "next_offset": next_off, "sha256": sha256, "error": None}


@_threaded_tool
def read_file(path: str, offset: int = 1, limit: int | None = None,
              line_numbers: bool = True) -> ReadResult2:
    """Read paginated UTF-8 text; /tmp maps to Termux TMPDIR.

    Relative paths resolve from HOME; /tmp maps to Termux TMPDIR. Returns sha256
    of the exact file bytes. offset is 1-indexed and limit caps lines before the
    server limits. line_numbers defaults true; its prefixes are display-only and
    must never be copied into edit match_text. Use line_numbers=false when copying
    source for edits, and use next_offset to continue.
    """
    return _read_file(path, offset, limit, line_numbers)


@_threaded_tool
def read_files(reads: list[ReadFileSpec]) -> dict:
    """Batch-read up to 20 text-file ranges in input order.

    Each item is {path: str, offset: int=1, limit: int|null=null,
    line_numbers: bool=true}. Relative paths resolve from HOME; /tmp maps to
    Termux TMPDIR. Number prefixes are display-only and must not be copied into
    edit match_text; use line_numbers=false to copy exact source. Each result has
    exact-byte sha256 for stale-write guards.
    """
    if not isinstance(reads, list) or not reads:
        return {"results": [], "error": "reads must be a non-empty array"}
    if len(reads) > 20:
        return {"results": [], "error": "reads accepts at most 20 items"}

    normalized = []
    for i, item in enumerate(reads):
        if isinstance(item, BaseModel):
            item = item.model_dump()
        if not isinstance(item, dict):
            return {"results": [], "error": f"reads[{i}] must be an object"}
        path = item.get("path")
        offset = item.get("offset", 1)
        limit = item.get("limit")
        line_numbers = item.get("line_numbers", True)
        if not isinstance(path, str) or not path:
            return {"results": [], "error": f"reads[{i}].path is required"}
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 1:
            return {"results": [], "error": f"reads[{i}].offset must be a positive integer"}
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            return {"results": [], "error": f"reads[{i}].limit must be a positive integer or null"}
        if not isinstance(line_numbers, bool):
            return {"results": [], "error": f"reads[{i}].line_numbers must be boolean"}
        normalized.append((path, offset, limit, line_numbers))

    return {"results": [
        {"path": path, **_read_file(path, offset, limit, line_numbers)}
        for path, offset, limit, line_numbers in normalized
    ], "error": None}


# ---------------------------------------------------------------------------
# Multi-file transaction: edit_files
# ---------------------------------------------------------------------------

def _canonicalize_paths(file_specs: list[dict]) -> list[tuple[pathlib.Path, dict]]:
    """Canonicalise paths and reject duplicate / symlink aliases.

    Resolves each path to its real filesystem location. If two different
    input paths resolve to the same canonical target (e.g. via symlink),
    a ValueError is raised.
    """
    seen: dict[str, str] = {}
    result = []
    for spec in file_specs:
        p = _tool_path(spec["path"])
        real = p.resolve(strict=False)
        key = str(real)
        if key in seen:
            raise ValueError(
                f"duplicate path: {spec['path']!r} resolves to same target as {seen[key]!r}")
        seen[key] = spec["path"]
        result.append((real, spec))
    return result


def _make_diff(base: str, new: str, path: str) -> str:
    return "".join(difflib.unified_diff(
        base.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=path, tofile=path, n=3))


def _publish_transaction(
    writes: list[tuple[pathlib.Path, bytes, bytes, int | None]]
) -> tuple[bool, str | None, list[dict]]:
    """Publish all writes atomically with best-effort rollback.

    writes: list of (canonical_path, new_bytes, old_bytes, old_mode).
    Each file is written via a same-directory temp file + os.replace.

    Process-crash boundary: os.replace is atomic per-file on the same
    filesystem. If the process crashes between os.replace calls, some files
    may be updated and others not. The transaction lock prevents concurrent
    transactions but cannot protect against process crashes between
    replacements. Full cross-file crash atomicity requires a higher-level
    journal/wal (not implemented in this stage).
    """
    completed: list[tuple[pathlib.Path, bytes, int | None]] = []
    temp_files: list[str] = []
    try:
        for canon, new_bytes, old_bytes, old_mode in writes:
            canon.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(prefix=f".{canon.name}.", dir=str(canon.parent))
            temp_files.append(temp)
            with os.fdopen(fd, "wb") as f:
                f.write(new_bytes)
                f.flush()
                os.fsync(f.fileno())
            if old_mode is not None:
                os.chmod(temp, old_mode)
            os.replace(temp, canon)
            completed.append((canon, old_bytes, old_mode))
            temp_files.remove(temp)
        return True, None, []
    except Exception as e:
        rollback_info: list[dict] = []
        for canon, old_bytes, old_mode in reversed(completed):
            try:
                if old_bytes is not None:
                    fd2, temp2 = tempfile.mkstemp(prefix=f".{canon.name}.", dir=str(canon.parent))
                    with os.fdopen(fd2, "wb") as f2:
                        f2.write(old_bytes)
                        f2.flush()
                        os.fsync(f2.fileno())
                    if old_mode is not None:
                        os.chmod(temp2, old_mode)
                    os.replace(temp2, canon)
                else:
                    canon.unlink(missing_ok=True)
                rollback_info.append({"path": str(canon), "restored": True})
            except Exception as re:
                rollback_info.append({"path": str(canon), "restored": False, "error": str(re)})
        for temp in temp_files:
            try: os.unlink(temp)
            except OSError: pass
        return False, str(e), rollback_info


# ---------------------------------------------------------------------------
# Transaction result builders
# ---------------------------------------------------------------------------

def _tx_error(error: str, rollback: list | None = None) -> dict:
    return {"ok": False, "applied": False, "dry_run": False, "files": [],
            "error": error, "rollback": rollback}


def _tx_result(file_data: list[dict], dry_run: bool, applied: bool,
               error: str | None, rollback: list | None = None) -> dict:
    files = [{
        "path": item["path"],
        "ok": not item.get("batch_error"),
        "sha256": item["sha256"],
        "result_sha256": item.get("new_sha256"),
        "changed": item.get("changed", False),
        "diff": item.get("diff") if not item.get("batch_error") else None,
        "results": item.get("results"),
        "batch_aborted": bool(item.get("batch_error")),
        "error": item.get("batch_error"),
    } for item in file_data]
    return {
        "ok": error is None,
        "applied": applied,
        "dry_run": dry_run,
        "files": files,
        "error": error,
        "rollback": rollback,
    }


# ---------------------------------------------------------------------------
# Core transaction runner (called under _TRANSACTION_LOCK)
# ---------------------------------------------------------------------------

def _run_transaction(file_specs: list, dry_run: bool) -> dict:
    """Validate every file, then preview or publish one atomic transaction."""
    normalized_specs = []
    for index, spec in enumerate(file_specs):
        if isinstance(spec, BaseModel):
            spec = spec.model_dump()
        if not isinstance(spec, dict):
            return _tx_error(f"files[{index}] must be an object")
        path = spec.get("path")
        edits = spec.get("edits")
        expected_sha = spec.get("expected_sha256")
        if not isinstance(path, str) or not path:
            return _tx_error(f"files[{index}].path is required")
        if not isinstance(edits, list) or not edits:
            if "content" in spec:
                return _tx_error(
                    f"files[{index}] edits existing UTF-8 files and requires a non-empty "
                    "edits array; use write_file to create or replace a file")
            return _tx_error(
                f"files[{index}].edits must be a non-empty array; {_EDIT_CONTRACT}")
        if expected_sha is not None and not isinstance(expected_sha, str):
            return _tx_error(f"files[{index}].expected_sha256 must be a string or null")
        normalized_specs.append({
            "path": path,
            "edits": edits,
            "expected_sha256": expected_sha,
        })

    try:
        canonical = _canonicalize_paths(normalized_specs)
    except ValueError as error:
        return _tx_error(str(error))

    file_data = []
    for canon, spec in canonical:
        try:
            raw = canon.read_bytes()
        except Exception as error:
            return _tx_error(f"cannot read {spec['path']}: {error}")

        source_sha = hashlib.sha256(raw).hexdigest()
        expected_sha = spec["expected_sha256"]
        if expected_sha is not None and not hmac.compare_digest(
                expected_sha.lower(), source_sha):
            return _tx_error(
                f"stale source for {spec['path']}: expected sha256 "
                f"{expected_sha}, got {source_sha}")

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            return _tx_error(f"file {spec['path']} is not valid UTF-8: {error}")

        bom, content = _strip_bom(text)
        ending = _detect_ending(content)
        normalized = _normalize_lf(content)
        old_mode = canon.stat().st_mode & 0o777
        try:
            base, new, results, batch_error = _apply_edits(
                normalized, spec["edits"], str(canon), diagnostic_content=content)
        except (KeyError, TypeError, ValueError) as error:
            return _tx_error(f"invalid edit in {spec['path']}: {error}")

        new_bytes = (bom + _restore_ending(new, ending)).encode("utf-8")
        reported_path = str(_tool_path(spec["path"]))
        file_data.append({
            "path": reported_path,
            "canonical": canon,
            "raw_bytes": raw,
            "sha256": source_sha,
            "old_mode": old_mode,
            "new_bytes": new_bytes,
            "new_sha256": hashlib.sha256(new_bytes).hexdigest(),
            "diff": _make_diff(base, new, reported_path),
            "results": results,
            "batch_error": batch_error,
            "changed": base != new,
        })

    if any(item["batch_error"] for item in file_data):
        return _tx_result(
            file_data, dry_run=False, applied=False,
            error="validation failed: one or more files have edit errors")
    if dry_run:
        return _tx_result(file_data, dry_run=True, applied=False, error=None)

    writes = [
        (item["canonical"], item["new_bytes"], item["raw_bytes"], item["old_mode"])
        for item in file_data if item["changed"]
    ]
    if not writes:
        return _tx_result(file_data, dry_run=False, applied=False, error=None)
    success, error, rollback = _publish_transaction(writes)
    if not success:
        return _tx_result(
            file_data, dry_run=False, applied=False,
            error=f"publication failed: {error}", rollback=rollback)
    return _tx_result(file_data, dry_run=False, applied=True, error=None)

@_serialized_threaded_tool
def edit_file(path: str, edits: list[EditSpec], dry_run: bool = False,
              expected_sha256: str | None = None) -> EditResult:
    """Atomically edit one existing UTF-8 file; this tool cannot create files.

    edits is a native array of {mode, match_text, write_text}. Canonical modes are
    replace_match, insert_before, and insert_after. Each non-empty match_text/anchor
    must occur uniquely. write_text is literal; insert modes never add a newline, so
    include it explicitly. Compatibility input accepts matchText/writeText and mode
    aliases insert_before_match/insert_after_match; output stays canonical snake_case.
    Use read_file(line_numbers=false), capture sha256, dry_run with expected_sha256,
    then apply the same payload/hash. Re-read after a stale error. Relative paths
    resolve from HOME; /tmp maps to Termux TMPDIR. Use write_file to create/replace.
    """
    transaction = _run_transaction([{
        "path": path,
        "edits": edits,
        "expected_sha256": expected_sha256,
    }], dry_run)
    if not transaction["files"]:
        return {
            "ok": False,
            "applied": transaction["applied"],
            "dry_run": transaction["dry_run"],
            "path": path,
            "replacements": 0,
            "changed": False,
            "diff": None,
            "results": None,
            "batch_aborted": False,
            "error": transaction["error"],
        }
    item = transaction["files"][0]
    replacements = sum(1 for result in item["results"] or [] if result["ok"])
    return {
        "ok": transaction["ok"] and item["ok"],
        "applied": transaction["applied"],
        "dry_run": transaction["dry_run"],
        "path": item["path"],
        "replacements": replacements if item["changed"] else 0,
        "changed": item["changed"],
        "diff": item["diff"],
        "results": item["results"],
        "batch_aborted": item["batch_aborted"],
        "error": transaction["error"] or item["error"],
    }


@_serialized_threaded_tool
def edit_files(files: list[EditFileSpec], dry_run: bool = False) -> dict:
    """Atomically edit multiple existing UTF-8 files; this cannot create files.

    Each item is {path: str, edits: EditSpec[], expected_sha256: str|null}. EditSpec
    uses canonical {mode, match_text, write_text}; modes are replace_match,
    insert_before, insert_after. Matches must be unique and insert text is literal,
    with no automatic newline. matchText/writeText and insert_before_match/
    insert_after_match are accepted compatibility aliases; output is canonical.
    All files validate before publication; dry_run previews diffs. Recommended flow:
    read_file(line_numbers=false) -> sha256 -> dry_run -> apply the same payload/hash;
    re-read if stale. Relative paths resolve from HOME; /tmp maps to Termux TMPDIR.
    Use write_file to create or replace files.
    """
    if not isinstance(files, list) or not files:
        return _tx_error("files must be a non-empty array")
    return _run_transaction(files, dry_run)


if __name__ == "__main__":
    auth_token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    app = mcp.streamable_http_app()
    if auth_token:
        app.add_middleware(AuthMiddleware, token=auth_token)
        print(f"[auth] token auth enabled ({auth_token[:4]}...{auth_token[-4:]})")
    if HOST not in ("127.0.0.1", "::1", "localhost") and not auth_token:
        print("[security] WARNING: non-loopback MCP_HOST without MCP_AUTH_TOKEN")
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
