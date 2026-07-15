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
from typing import TypedDict

from mcp.server.fastmcp import FastMCP

READ_MAX_LINES = int(os.environ.get("MCP_READ_MAX_LINES", "2000"))
READ_MAX_BYTES = int(os.environ.get("MCP_READ_MAX_BYTES", str(50 * 1024)))

HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8088"))
TRUNC_LIMIT = int(os.environ.get("MCP_TRUNC_LIMIT", "8192"))
MAX_SESSIONS = int(os.environ.get("MCP_MAX_SESSIONS", "50"))
TEMP_ROOT = pathlib.Path(os.environ.get("TMPDIR") or tempfile.gettempdir()).resolve()


# session_id -> {"stdout": bytes, "stderr": bytes}
_buffers: "OrderedDict[str, dict]" = OrderedDict()

# Serialises validation+publication so concurrent stale commits cannot race.
_TRANSACTION_LOCK = threading.RLock()

mcp = FastMCP("termux-shell", host=HOST, port=PORT)


def _tool_path(path: str) -> pathlib.Path:
    """Map Android's missing /tmp namespace to Termux's writable TMPDIR."""
    expanded = pathlib.Path(path).expanduser()
    if path != "/tmp" and not path.startswith("/tmp/"):
        return expanded
    root = TEMP_ROOT.resolve(strict=False)
    relative = pathlib.PurePosixPath(path).relative_to("/tmp")
    mapped = root.joinpath(*relative.parts).resolve(strict=False)
    if mapped != root and root not in mapped.parents:
        raise ValueError("/tmp path escapes the temporary directory")
    return mapped


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
    buffer readable via read_output using the returned session_id. A cwd under
    /tmp is mapped to Termux's TMPDIR; command text is not rewritten.
    """
    try:
        resolved_cwd = str(_tool_path(cwd)) if cwd is not None else None
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


def _fuzzy(t: str) -> str:
    t = unicodedata.normalize("NFKC", t)
    t = "\n".join(line.rstrip() for line in t.split("\n"))
    t = _SMART_SINGLE.sub("'", t)
    t = _SMART_DOUBLE.sub('"', t)
    t = _DASHES.sub("-", t)
    t = _SPACES.sub(" ", t)
    return t


def _find(content: str, old: str) -> tuple[bool, int, int, bool]:
    i = content.find(old)
    if i != -1:
        return True, i, len(old), False
    fc, fo = _fuzzy(content), _fuzzy(old)
    fi = fc.find(fo)
    if fi == -1:
        return False, -1, 0, False
    return True, fi, len(fo), True


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


def _match_one(base: str, used_fuzzy: bool, old: str, new: str, path: str, label: str):
    """Resolve one edit: exact/trailing-fuzzy, then indent-insensitive with re-indent."""
    found, idx, mlen, _ = _find(base, old)
    if found:
        fo = _fuzzy(old) if used_fuzzy else old
        occ = base.count(fo)
        if occ > 1:
            raise ValueError(f"Found {occ} occurrences of {label} in {path}. Each match_text must "
                             f"be unique. Provide more context.")
        return idx, mlen, new
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

def _normalize_edit(e: dict) -> dict:
    """Validate one explicit edit spec and normalise line endings."""
    if not isinstance(e, dict):
        raise ValueError("edit must be an object")
    mode = e.get("mode")
    if mode not in ("replace_match", "insert_before", "insert_after"):
        raise ValueError(f"unknown edit mode: {mode!r}")
    match_text = e.get("match_text", "")
    write_text = e.get("write_text", "")
    if not isinstance(match_text, str) or not isinstance(write_text, str):
        raise ValueError("match_text and write_text must be strings")
    if not match_text:
        raise ValueError("match_text must be non-empty")
    return {
        "mode": mode,
        "match_text": _normalize_lf(match_text),
        "write_text": _normalize_lf(write_text),
    }

def _resolve_insert(base: str, used_fuzzy: bool, mode: str, anchor: str,
                    content: str, path: str, label: str) -> tuple[int, int, str]:
    """Resolve an insert edit. Returns (insert_index, 0, content).

    Anchor must be unique. Content is literal; no newline is added. When the
    anchor is matched indent-insensitively, content is reindented to the
    anchor's actual indentation.
    """
    reindented = content
    found, idx, mlen, _ = _find(base, anchor)
    if found:
        fo = _fuzzy(anchor) if used_fuzzy else anchor
        occ = base.count(fo)
        if occ > 1:
            raise ValueError(f"{label}: anchor found {occ} times in {path}. Anchor must be unique.")
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

def _closest_match(content: str, search: str, max_excerpt: int = 500) -> dict:
    """Find the closest matching line for no-match diagnostics."""
    search_first = search.strip().split("\n")[0] if search.strip() else search
    if not search_first:
        return {}
    content_lines = content.split("\n")
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
        lines.append(f"{prefix} {j + 1}: {content_lines[j]}")
    return {
        "closest_match_line": best_line + 1,
        "similarity": round(best_ratio, 3),
        "nearby_text": "\n".join(lines)[:max_excerpt],
    }


def _ambiguity_info(content: str, search: str, max_candidates: int = 5) -> dict:
    """Return bounded candidate lines for ambiguous-match diagnostics."""
    content_lines = content.split("\n")
    search_first = search.strip().split("\n")[0] if search.strip() else search
    if not search_first:
        return {}
    candidates = []
    for i, line in enumerate(content_lines):
        if search_first in line:
            candidates.append({"line": i + 1, "text": line.strip()[:200]})
        if len(candidates) >= max_candidates:
            break
    return {"candidate_lines": candidates} if candidates else {}


def _apply_edits(normalized: str, edits: list[dict], path: str
                 ) -> tuple[str, str, list[dict], str | None]:
    """Resolve every edit first; any failure aborts the whole file."""
    norm = [_normalize_edit(e) for e in edits]
    search_texts = [e["match_text"] for e in norm]
    used_fuzzy = any(_find(normalized, text)[3] for text in search_texts)
    original = normalized
    base = _fuzzy(normalized) if used_fuzzy else normalized
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
                    base, used_fuzzy, edit["match_text"], edit["write_text"], path, label)
            else:
                idx, match_len, new_text = _resolve_insert(
                    base, used_fuzzy, edit["mode"], edit["match_text"],
                    edit["write_text"], path, label)
            result["match_count"] = 1
            matched.append({"i": i, "idx": idx, "len": match_len, "new": new_text})
            result.update(matched=True, ok=True, status="matched")
        except ValueError as error:
            result["reason"] = str(error)
            result["error"] = str(error)
            occurrences = original.count(edit["match_text"])
            result["match_count"] = occurrences
            if occurrences == 0:
                result.update(_closest_match(original, edit["match_text"]))
            elif occurrences > 1:
                result.update(_ambiguity_info(original, edit["match_text"]))
        results.append(result)

    failure = next((result for result in results if not result["ok"]), None)
    ordered = sorted(matched, key=lambda item: item["idx"])
    for left, right in zip(ordered, ordered[1:]):
        if left["idx"] + left["len"] > right["idx"]:
            message = f"edits[{left['i']}] and edits[{right['i']}] overlap in {path}. Merge them."
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
def write_file(path: str, content: str) -> WriteResult:
    """Atomically write UTF-8 content, creating parents; /tmp maps to Termux TMPDIR."""
    try:
        p = _tool_path(path)
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

    Returns sha256 of the exact file bytes. offset is 1-indexed and limit caps
    lines before the server's line/byte limits. Use next_offset to continue.
    """
    return _read_file(path, offset, limit, line_numbers)


@_threaded_tool
def read_files(reads: list) -> dict:
    """Batch-read up to 20 text-file ranges in input order.

    Each item accepts path, offset, limit, and line_numbers with read_file
    semantics. Pass a native array.
    """
    if not isinstance(reads, list) or not reads:
        return {"results": [], "error": "reads must be a non-empty array"}
    if len(reads) > 20:
        return {"results": [], "error": "reads accepts at most 20 items"}

    normalized = []
    for i, item in enumerate(reads):
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
        if not isinstance(spec, dict):
            return _tx_error(f"files[{index}] must be an object")
        path = spec.get("path")
        edits = spec.get("edits")
        expected_sha = spec.get("expected_sha256")
        if not isinstance(path, str) or not path:
            return _tx_error(f"files[{index}].path is required")
        if not isinstance(edits, list) or not edits:
            return _tx_error(f"files[{index}].edits must be a non-empty array")
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
                normalized, spec["edits"], str(canon))
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
def edit_file(path: str, edits: list, dry_run: bool = False,
              expected_sha256: str | None = None) -> EditResult:
    """Atomically edit one UTF-8 file; /tmp maps to Termux TMPDIR."""
    transaction = _run_transaction([{
        "path": path,
        "edits": edits,
        "expected_sha256": expected_sha256,
    }], dry_run)
    if not transaction["files"]:
        return {
            "ok": False,
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
        "path": item["path"],
        "replacements": replacements if item["changed"] else 0,
        "changed": item["changed"],
        "diff": item["diff"],
        "results": item["results"],
        "batch_aborted": item["batch_aborted"],
        "error": transaction["error"] or item["error"],
    }


@_serialized_threaded_tool
def edit_files(files: list, dry_run: bool = False) -> dict:
    """Atomically edit UTF-8 files; /tmp maps to Termux TMPDIR."""
    if not isinstance(files, list) or not files:
        return _tx_error("files must be a non-empty array")
    return _run_transaction(files, dry_run)


if __name__ == "__main__":
    auth_token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    app = mcp.streamable_http_app()
    if auth_token:
        app.add_middleware(AuthMiddleware, token=auth_token)
        print(f"[auth] token auth enabled ({auth_token[:4]}...{auth_token[-4:]})")
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
