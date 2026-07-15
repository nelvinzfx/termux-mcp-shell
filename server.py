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
import time
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

_PLAN_TTL = int(os.environ.get("EDIT_PLAN_TTL_SECONDS", "600"))
_PLAN_CAPACITY = int(os.environ.get("EDIT_PLAN_CAPACITY", "50"))

# session_id -> {"stdout": bytes, "stderr": bytes}
_buffers: "OrderedDict[str, dict]" = OrderedDict()

# Plan store: plan_id -> {files, created_at, consumed}
_PLANS: "OrderedDict[str, dict]" = OrderedDict()
# Removed plan tracking: plan_id -> reason ("evicted" | "expired")
_REMOVED: "OrderedDict[str, str]" = OrderedDict()
_PLAN_LOCK = threading.Lock()
# Serialises validation+publication so concurrent stale commits cannot race.
_TRANSACTION_LOCK = threading.RLock()

mcp = FastMCP("termux-shell", host=HOST, port=PORT)


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
    buffer readable via read_output using the returned session_id.
    """
    spawn_task = asyncio.create_task(asyncio.create_subprocess_shell(
        command, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        executable="/bin/sh", start_new_session=True,
    ))
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
    """Re-indent new_text by the indent delta between the matched block and what the model sent.

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
            raise ValueError(f"Found {occ} occurrences of {label} in {path}. Each old_text must "
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
    """Normalise an edit spec to canonical form.

    Accepts:
      - legacy {old_text, new_text}            -> replace_match
      - {mode:"replace_match", match_text, write_text}
      - {mode:"insert_before", anchor, content}
      - {mode:"insert_after",  anchor, content}
    """
    if not isinstance(e, dict):
        raise ValueError("edit must be an object")

    if "old_text" in e or "new_text" in e:
        if "mode" in e:
            raise ValueError("cannot specify both mode and old_text/new_text")
        old = e.get("old_text", "")
        new = e.get("new_text", "")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError("old_text and new_text must be strings")
        return {"mode": "replace_match",
                "match_text": _normalize_lf(old),
                "write_text": _normalize_lf(new)}

    mode = e.get("mode")
    if mode == "replace_match":
        mt = e.get("match_text", "")
        wt = e.get("write_text", "")
        if not isinstance(mt, str) or not isinstance(wt, str):
            raise ValueError("match_text and write_text must be strings")
        return {"mode": "replace_match",
                "match_text": _normalize_lf(mt),
                "write_text": _normalize_lf(wt)}
    elif mode in ("insert_before", "insert_after"):
        anchor = e.get("anchor", "")
        content = e.get("content", "")
        if not isinstance(anchor, str) or not isinstance(content, str):
            raise ValueError("anchor and content must be strings")
        if not anchor.strip():
            raise ValueError(f"anchor must be non-empty for {mode}")
        return {"mode": mode,
                "anchor": _normalize_lf(anchor),
                "content": _normalize_lf(content)}
    else:
        raise ValueError(f"unknown edit mode: {mode!r}")


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


def _apply_edits(normalized: str, edits: list[dict], path: str,
                 partial: bool = False) -> tuple[str, str, list[dict], str | None]:
    """Resolve every edit before applying any, retaining atomic-failure diagnostics.

    Supports replace_match (legacy old_text/new_text or explicit mode),
    insert_before, and insert_after. All edits are resolved against the
    (possibly fuzzified) base before any are applied, so a failure in one
    edit aborts the entire batch (unless partial=True).
    """
    norm = [_normalize_edit(e) for e in edits]
    search_texts = [e["match_text"] if e["mode"] == "replace_match" else e["anchor"] for e in norm]
    used_fuzzy = any(_find(normalized, t)[3] for t in search_texts if t)
    orig = normalized  # original for diagnostics (before fuzzification)
    base = _fuzzy(normalized) if used_fuzzy else normalized
    matched, results = [], []
    for i, e in enumerate(norm):
        r = {"index": i, "mode": e["mode"], "matched": False, "ok": False,
             "status": "failed", "reason": None, "error": None, "match_count": None}
        label = f"edits[{i}]" if len(norm) > 1 else "the text"
        try:
            if e["mode"] == "replace_match":
                if not e["match_text"]:
                    raise ValueError(f"{label}: match_text is empty")
                idx, mlen, newtext = _match_one(base, used_fuzzy, e["match_text"],
                                                 e["write_text"], path, label)
            else:
                idx, mlen, newtext = _resolve_insert(base, used_fuzzy, e["mode"],
                                                      e["anchor"], e["content"], path, label)
            r["match_count"] = 1
            matched.append({"i": i, "idx": idx, "len": mlen, "new": newtext})
            r.update(matched=True, ok=True, status="matched")
        except ValueError as err:
            r["reason"] = str(err)
            r["error"] = str(err)
            search = e.get("match_text") or e.get("anchor", "")
            if search:
                occ = orig.count(search)
                r["match_count"] = occ
                if occ == 0:
                    diag = _closest_match(orig, search)
                    if diag:
                        r.update(diag)
                elif occ > 1:
                    diag = _ambiguity_info(orig, search)
                    if diag:
                        r.update(diag)
        results.append(r)
    failure = next((r for r in results if not r["ok"]), None)
    ordered = sorted(matched, key=lambda m: m["idx"])
    for a, b in zip(ordered, ordered[1:]):
        if a["idx"] + a["len"] > b["idx"]:
            msg = f"edits[{a['i']}] and edits[{b['i']}] overlap in {path}. Merge them."
            r = results[b["i"]]; r.update(matched=False, ok=False, status="failed", reason=msg, error=msg)
            failure = failure or r
            matched = [m for m in matched if m["i"] != b["i"]]
    if failure and not partial:
        for r in results:
            if r["ok"]: r["status"] = "aborted"
        reason = failure.get("reason") or "edit failed"
        return base, base, results, f"atomic batch aborted: {reason}"
    new = base
    for m in sorted(matched, key=lambda m: m["idx"], reverse=True):
        new = new[:m["idx"]] + m["new"] + new[m["idx"] + m["len"]:]
    for r in results:
        if r["ok"]: r["status"] = "applied" if new != base else "matched_no_change"
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
def write_file(path: str, content: str, sha256: str | None = None) -> WriteResult:
    """Atomically write UTF-8 content, creating parent directories; optionally verify SHA-256."""
    try:
        p = pathlib.Path(path).expanduser(); data = content.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        if sha256 is not None and not hmac.compare_digest(sha256.lower(), digest):
            return {"ok": False, "path": str(p), "bytes_written": 0, "sha256": digest, "error": f"sha256 mismatch: expected {sha256}, got {digest}"}
        _atomic_write(p, data)
        return {"ok": True, "path": str(p), "bytes_written": len(data), "sha256": digest, "error": None}
    except Exception as e:
        return {"ok": False, "path": path, "bytes_written": 0, "sha256": None, "error": str(e)}


@_serialized_threaded_tool
def append_file(path: str, content: str, sha256: str | None = None) -> WriteResult:
    """Atomically append UTF-8 content, creating the file and parent directories if needed."""
    try:
        p = pathlib.Path(path).expanduser(); addition = content.encode("utf-8")
        data = (p.read_bytes() if p.exists() else b"") + addition
        digest = hashlib.sha256(data).hexdigest()
        if sha256 is not None and not hmac.compare_digest(sha256.lower(), digest):
            return {"ok": False, "path": str(p), "bytes_written": 0, "sha256": digest, "error": f"sha256 mismatch: expected {sha256}, got {digest}"}
        _atomic_write(p, data)
        return {"ok": True, "path": str(p), "bytes_written": len(addition), "sha256": digest, "error": None}
    except Exception as e:
        return {"ok": False, "path": path, "bytes_written": 0, "sha256": None, "error": str(e)}


# ---------------------------------------------------------------------------
# edit_file (single-file, backward-compatible)
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
    content: str
    start_line: int
    end_line: int
    total_lines: int
    truncated: bool
    next_offset: int | None
    sha256: str | None
    error: str | None


@_serialized_threaded_tool
def edit_file(path: str, edits: "str | list", dry_run: bool = False,
              partial: bool = False, expected_sha256: str | None = None) -> EditResult:
    """Atomically apply replacements; accepts legacy JSON-string or native-array edits.

    Supports old_text/new_text (legacy) and explicit modes: replace_match,
    insert_before, insert_after. Optional expected_sha256 provides stale-source
    protection: if the file's current SHA-256 does not match, no edits are
    attempted.
    """
    _err = lambda e: {"ok": False, "path": path, "replacements": 0, "changed": False,
                       "diff": None, "results": None, "batch_aborted": False, "error": e}
    if isinstance(edits, str):
        try: edits = json.loads(edits)
        except (json.JSONDecodeError, TypeError) as e:
            return _err(f"edits is not valid JSON: {e}")
    if not isinstance(edits, list) or not edits:
        return _err("edits must be a non-empty JSON array of {old_text, new_text} objects.")
    try:
        p = pathlib.Path(path).expanduser(); raw = p.read_bytes()
    except Exception as e:
        return _err(str(e))
    source_sha = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and not hmac.compare_digest(expected_sha256.lower(), source_sha):
        return _err(f"stale source: expected sha256 {expected_sha256}, got {source_sha}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return _err(f"file is not valid UTF-8: {e}")
    bom, content = _strip_bom(text); ending = _detect_ending(content); normalized = _normalize_lf(content)
    try:
        base, new, results, batch_error = _apply_edits(normalized, edits, path, partial=partial)
    except (KeyError, TypeError, ValueError) as e:
        return _err(f"invalid edit: {e}")
    changed = base != new
    diff = "".join(difflib.unified_diff(base.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=path, tofile=path, n=3))
    if batch_error:
        return {"ok": False, "path": str(p), "replacements": 0, "changed": False, "diff": None, "results": results, "batch_aborted": True, "error": batch_error}
    applied = sum(1 for r in results if r["ok"])
    if dry_run or not changed:
        return {"ok": True, "path": str(p), "replacements": 0 if not changed else applied, "changed": changed, "diff": diff, "results": results, "batch_aborted": False, "error": None}
    try:
        _atomic_write(p, (bom + _restore_ending(new, ending)).encode("utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(p), "replacements": 0, "changed": False, "diff": None, "results": results, "batch_aborted": False, "error": str(e)}
    return {"ok": True, "path": str(p), "replacements": applied, "changed": True, "diff": diff, "results": results, "batch_aborted": False, "error": None}


@_threaded_tool
def read_file_bytes(path: str, offset: int = 0, length: int = 4096) -> dict:
    """Read a byte range, returning base64 data so binary and minified files are safe."""
    if offset < 0 or length < 0:
        return {"ok": False, "path": path, "offset": offset, "length": 0, "total_bytes": 0, "eof": True, "data_base64": "", "error": "offset and length must be non-negative"}
    try:
        p = pathlib.Path(path).expanduser(); total = p.stat().st_size
        with p.open("rb") as f: f.seek(offset); data = f.read(length)
        return {"ok": True, "path": str(p), "offset": offset, "length": len(data), "total_bytes": total, "eof": offset + len(data) >= total, "data_base64": base64.b64encode(data).decode("ascii"), "error": None}
    except Exception as e:
        return {"ok": False, "path": path, "offset": offset, "length": 0, "total_bytes": 0, "eof": True, "data_base64": "", "error": str(e)}


def _read_file(path: str, offset: int = 1, limit: int | None = None,
               line_numbers: bool = True) -> ReadResult2:
    try:
        p = pathlib.Path(path).expanduser()
        raw_bytes = p.read_bytes()
        sha256 = hashlib.sha256(raw_bytes).hexdigest()
        text = raw_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        return {"content": "", "start_line": offset, "end_line": 0, "total_lines": 0,
                "truncated": False, "next_offset": None, "sha256": None, "error": str(e)}
    lines = text.split("\n")
    final_newline = bool(lines and lines[-1] == "")
    if final_newline:
        lines.pop()
    total = len(lines)
    start = max(0, offset - 1)
    if start >= total:
        return {"content": "", "start_line": offset, "end_line": 0, "total_lines": total,
                "truncated": False, "next_offset": None, "sha256": sha256,
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
    return {"content": body, "start_line": start + 1, "end_line": start + n,
            "total_lines": total, "truncated": truncated, "next_offset": next_off,
            "sha256": sha256, "error": None}


@_threaded_tool
def read_file(path: str, offset: int = 1, limit: int | None = None,
              line_numbers: bool = True) -> ReadResult2:
    """Read paginated UTF-8 text; set line_numbers=false for exact raw text.

    Returns sha256 of the exact file bytes. offset is 1-indexed and limit caps
    lines before the server's line/byte limits. Use next_offset to continue.
    """
    return _read_file(path, offset, limit, line_numbers)


@_threaded_tool
def read_files(reads: "str | list") -> dict:
    """Batch-read up to 20 text-file ranges in input order.

    Each item accepts path, offset, limit, and line_numbers with read_file
    semantics. Pass a native array or JSON-encoded array.
    """
    if isinstance(reads, str):
        try:
            reads = json.loads(reads)
        except (json.JSONDecodeError, TypeError) as e:
            return {"results": [], "error": f"reads is not valid JSON: {e}"}
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
        p = pathlib.Path(spec["path"]).expanduser()
        real = p.resolve(strict=False)
        key = str(real)
        if key in seen:
            raise ValueError(
                f"duplicate path: {spec['path']!r} resolves to same target as {seen[key]!r}")
        seen[key] = spec["path"]
        result.append((real, spec))
    return result


def _read_file_for_edit(canon: pathlib.Path, display_path: str) -> tuple[bytes, str, str, str, str, int]:
    """Read a file for editing. Returns (raw, sha256, bom, ending, normalized, old_mode)."""
    raw = canon.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8")  # raises UnicodeDecodeError if binary
    bom, content = _strip_bom(text)
    ending = _detect_ending(content)
    normalized = _normalize_lf(content)
    old_mode = canon.stat().st_mode & 0o777
    return raw, sha256, bom, ending, normalized, old_mode


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
# Plan store (process-local, thread-safe, TTL/capacity bounded)
# ---------------------------------------------------------------------------

def _track_removed(plan_id: str, reason: str) -> None:
    _REMOVED[plan_id] = reason
    while len(_REMOVED) > _PLAN_CAPACITY:
        _REMOVED.popitem(last=False)


def _evict_oldest_plan() -> None:
    while len(_PLANS) >= _PLAN_CAPACITY:
        old_id, _ = _PLANS.popitem(last=False)
        _track_removed(old_id, "evicted")


def _store_plan(file_data: list[dict]) -> str:
    """Store a validated transaction as an opaque plan. Returns plan_id."""
    plan_id = uuid.uuid4().hex
    plan = {
        "files": [
            {
                "path": fd["path"],
                "canonical": str(fd["canonical"]),
                "normalized_edits": fd["normalized_edits"],
                "source_sha256": fd["sha256"],
            }
            for fd in file_data
        ],
        "created_at": time.monotonic(),
        "consumed": False,
    }
    with _PLAN_LOCK:
        _evict_oldest_plan()
        _PLANS[plan_id] = plan
    return plan_id


def _check_plan(plan_id: str) -> tuple[dict | None, str | None]:
    """Check plan status. Returns (plan, error).

    Distinct errors: expired, reused, evicted, missing.
    Stale (hash mismatch) is detected later during revalidation.
    """
    with _PLAN_LOCK:
        plan = _PLANS.get(plan_id)
        if plan is not None:
            if time.monotonic() - plan["created_at"] > _PLAN_TTL:
                _PLANS.pop(plan_id, None)
                _track_removed(plan_id, "expired")
                return None, "expired"
            if plan["consumed"]:
                return None, "reused"
            return plan, None
        reason = _REMOVED.get(plan_id)
        if reason is not None:
            return None, reason  # "evicted" or "expired"
        return None, "missing"


def _consume_plan(plan_id: str) -> None:
    with _PLAN_LOCK:
        if plan_id in _PLANS:
            _PLANS[plan_id]["consumed"] = True


# ---------------------------------------------------------------------------
# Transaction result builders
# ---------------------------------------------------------------------------

def _tx_error(error: str, plan_id: str | None = None,
              rollback: list | None = None) -> dict:
    return {"ok": False, "applied": False, "dry_run": False, "files": [],
            "plan_id": plan_id, "error": error, "rollback": rollback}


def _tx_result(file_data: list[dict], dry_run: bool, applied: bool,
               error: str | None, plan_id: str | None = None,
               rollback: list | None = None) -> dict:
    files = []
    for fd in file_data:
        files.append({
            "path": fd["path"],
            "ok": not fd.get("batch_error"),
            "sha256": fd["sha256"],
            "result_sha256": fd.get("new_sha256"),
            "changed": fd.get("changed", False),
            "diff": fd.get("diff") if not fd.get("batch_error") else None,
            "results": fd.get("results"),
            "batch_aborted": bool(fd.get("batch_error")),
            "error": fd.get("batch_error"),
        })
    return {
        "ok": error is None,
        "applied": applied,
        "dry_run": dry_run,
        "files": files,
        "plan_id": plan_id,
        "error": error,
        "rollback": rollback,
    }


# ---------------------------------------------------------------------------
# Core transaction runner (called under _TRANSACTION_LOCK)
# ---------------------------------------------------------------------------

def _run_transaction(file_specs: list, dry_run: bool, return_diff: bool,
                     validate_all: bool, create_plan: bool) -> dict:
    """Validate, optionally plan, and publish a multi-file edit transaction."""

    # 1. Normalise file specs
    normalized_specs: list[dict] = []
    for i, spec in enumerate(file_specs):
        if not isinstance(spec, dict):
            return _tx_error(f"files[{i}] must be an object")
        fpath = spec.get("path")
        if not fpath or not isinstance(fpath, str):
            return _tx_error(f"files[{i}].path is required")
        edits = spec.get("edits")
        if isinstance(edits, str):
            try:
                edits = json.loads(edits)
            except (json.JSONDecodeError, TypeError) as e:
                return _tx_error(f"files[{i}].edits is not valid JSON: {e}")
        if not isinstance(edits, list) or not edits:
            return _tx_error(f"files[{i}].edits must be a non-empty array")
        expected_sha = spec.get("expected_sha256")
        normalized_specs.append({"path": fpath, "edits": edits, "expected_sha256": expected_sha})

    # 2. Canonicalise paths, reject duplicates
    try:
        canonical = _canonicalize_paths(normalized_specs)
    except ValueError as e:
        return _tx_error(str(e))

    # 3. Read, validate hashes, resolve edits for each file
    file_data: list[dict] = []
    for canon, spec in canonical:
        try:
            raw = canon.read_bytes()
        except Exception as e:
            return _tx_error(f"cannot read {spec['path']}: {e}")

        sha256 = hashlib.sha256(raw).hexdigest()
        if spec["expected_sha256"] is not None:
            if not hmac.compare_digest(spec["expected_sha256"].lower(), sha256):
                return _tx_error(
                    f"stale source for {spec['path']}: expected sha256 "
                    f"{spec['expected_sha256']}, got {sha256}")

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            return _tx_error(f"file {spec['path']} is not valid UTF-8: {e}")

        bom, content = _strip_bom(text)
        ending = _detect_ending(content)
        normalized = _normalize_lf(content)
        old_mode = canon.stat().st_mode & 0o777

        # Pre-normalise edits for plan storage
        try:
            normalized_edits = [_normalize_edit(e) for e in spec["edits"]]
        except ValueError as e:
            return _tx_error(f"invalid edit in {spec['path']}: {e}")

        try:
            base, new, results, batch_error = _apply_edits(
                normalized, spec["edits"], str(canon))
        except (KeyError, TypeError, ValueError) as e:
            return _tx_error(f"invalid edit in {spec['path']}: {e}")

        new_bytes = (bom + _restore_ending(new, ending)).encode("utf-8")
        new_sha256 = hashlib.sha256(new_bytes).hexdigest()
        diff = _make_diff(base, new, spec["path"]) if return_diff else ""

        file_data.append({
            "path": spec["path"],
            "canonical": canon,
            "raw_bytes": raw,
            "sha256": sha256,
            "bom": bom,
            "ending": ending,
            "old_mode": old_mode,
            "normalized_edits": normalized_edits,
            "base": base,
            "new": new,
            "new_bytes": new_bytes,
            "new_sha256": new_sha256,
            "diff": diff,
            "results": results,
            "batch_error": batch_error,
            "changed": base != new,
        })

    # 4. Check for failures
    has_failure = any(fd["batch_error"] for fd in file_data)
    if has_failure and validate_all:
        return _tx_result(file_data, dry_run=False, applied=False,
                          error="validation failed: one or more files have edit errors")

    # 5. Dry run
    if dry_run:
        result = _tx_result(file_data, dry_run=True, applied=False, error=None)
        if create_plan and not has_failure:
            result["plan_id"] = _store_plan(file_data)
        return result

    # 6. Publish
    if has_failure and not validate_all:
        writes = [(fd["canonical"], fd["new_bytes"], fd["raw_bytes"], fd["old_mode"])
                  for fd in file_data if not fd["batch_error"] and fd["changed"]]
    else:
        writes = [(fd["canonical"], fd["new_bytes"], fd["raw_bytes"], fd["old_mode"])
                  for fd in file_data if fd["changed"]]

    if not writes:
        return _tx_result(file_data, dry_run=False, applied=False, error=None)

    success, error, rollback = _publish_transaction(writes)
    if not success:
        return _tx_result(file_data, dry_run=False, applied=False,
                          error=f"publication failed: {error}", rollback=rollback)
    return _tx_result(file_data, dry_run=False, applied=True, error=None)


def _apply_plan_by_id(plan_id: str, dry_run: bool, return_diff: bool) -> dict:
    """Apply a stored plan by ID. Revalidates source hashes under the commit lock."""

    with _TRANSACTION_LOCK:
        # Check plan status under both locks
        with _PLAN_LOCK:
            plan = _PLANS.get(plan_id)
            if plan is None:
                reason = _REMOVED.get(plan_id, "missing")
                return _tx_error(f"plan {plan_id}: {reason}", plan_id=plan_id)
            if time.monotonic() - plan["created_at"] > _PLAN_TTL:
                _PLANS.pop(plan_id, None)
                _track_removed(plan_id, "expired")
                return _tx_error(f"plan {plan_id}: expired", plan_id=plan_id)
            if plan["consumed"]:
                return _tx_error(f"plan {plan_id}: reused", plan_id=plan_id)

        # Revalidate source hashes and resolve edits
        file_data: list[dict] = []
        for pf in plan["files"]:
            canon = pathlib.Path(pf["canonical"])
            try:
                raw = canon.read_bytes()
            except Exception as e:
                return _tx_error(f"cannot read {pf['path']}: {e}", plan_id=plan_id)

            sha256 = hashlib.sha256(raw).hexdigest()
            if sha256 != pf["source_sha256"]:
                return _tx_error(
                    f"stale source for {pf['path']}: expected sha256 "
                    f"{pf['source_sha256']}, got {sha256}",
                    plan_id=plan_id)

            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as e:
                return _tx_error(
                    f"file {pf['path']} is not valid UTF-8: {e}", plan_id=plan_id)

            bom, content = _strip_bom(text)
            ending = _detect_ending(content)
            normalized = _normalize_lf(content)
            old_mode = canon.stat().st_mode & 0o777

            try:
                base, new, results, batch_error = _apply_edits(
                    normalized, pf["normalized_edits"], str(canon))
            except (KeyError, TypeError, ValueError) as e:
                return _tx_error(f"invalid edit in {pf['path']}: {e}", plan_id=plan_id)

            new_bytes = (bom + _restore_ending(new, ending)).encode("utf-8")
            new_sha256 = hashlib.sha256(new_bytes).hexdigest()
            diff = _make_diff(base, new, pf["path"]) if return_diff else ""

            file_data.append({
                "path": pf["path"],
                "canonical": canon,
                "raw_bytes": raw,
                "sha256": sha256,
                "bom": bom,
                "ending": ending,
                "old_mode": old_mode,
                "base": base,
                "new": new,
                "new_bytes": new_bytes,
                "new_sha256": new_sha256,
                "diff": diff,
                "results": results,
                "batch_error": batch_error,
                "changed": base != new,
            })

        # Check for edit failures (shouldn't happen if hashes match, but be safe)
        has_failure = any(fd["batch_error"] for fd in file_data)
        if has_failure:
            return _tx_result(file_data, dry_run=dry_run, applied=False,
                              error="plan revalidation failed: edit errors",
                              plan_id=plan_id)

        if dry_run:
            return _tx_result(file_data, dry_run=True, applied=False,
                              error=None, plan_id=plan_id)

        # Publish
        writes = [(fd["canonical"], fd["new_bytes"], fd["raw_bytes"], fd["old_mode"])
                  for fd in file_data if fd["changed"]]

        if not writes:
            _consume_plan(plan_id)
            return _tx_result(file_data, dry_run=False, applied=False,
                              error=None, plan_id=plan_id)

        success, error, rollback = _publish_transaction(writes)
        if not success:
            # Failed apply: plan remains available until expiry.
            return _tx_result(file_data, dry_run=False, applied=False,
                              error=f"publication failed: {error}",
                              plan_id=plan_id, rollback=rollback)

        # Successful apply: consume one-shot.
        _consume_plan(plan_id)
        return _tx_result(file_data, dry_run=False, applied=True,
                          error=None, plan_id=plan_id)


@_threaded_tool
def edit_files(files: "str | list | None" = None, dry_run: bool = False,
               return_diff: bool = True, validate_all: bool = True,
               create_plan: bool = False,
               apply_plan: "str | None" = None) -> dict:
    """Atomic multi-file edit transaction with dry-run, plan, and rollback support.

    Parameters:
      files: list of {path, edits, expected_sha256?} or a JSON string thereof.
      dry_run: validate all and return diagnostics without writing.
      return_diff: include unified diff per file.
      validate_all: if True (default), any file's edit failure aborts the
        entire transaction (no writes). If False, successful files are
        still applied.
      create_plan: with dry_run, store the validated transaction and return
        an opaque plan_id for later apply_plan.
      apply_plan: apply a previously created plan by ID. Must be used alone
        (no files payload). Revalidates source hashes under the commit lock.

    Returns a structured result with per-file diagnostics, diffs, source and
    result SHA-256 hashes, and rollback info on publication failure.
    """
    # Apply-plan mode: reject simultaneous files payload
    if apply_plan is not None:
        if files is not None:
            if isinstance(files, str) and files.strip():
                return _tx_error("cannot specify both files payload and apply_plan",
                                 plan_id=apply_plan)
            if isinstance(files, list) and files:
                return _tx_error("cannot specify both files payload and apply_plan",
                                 plan_id=apply_plan)
        return _apply_plan_by_id(apply_plan, dry_run, return_diff)

    # Normal files-payload mode
    if files is None:
        return _tx_error("no files provided and no apply_plan specified")
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except (json.JSONDecodeError, TypeError) as e:
            return _tx_error(f"files is not valid JSON: {e}")
    if not isinstance(files, list) or not files:
        return _tx_error("files must be a non-empty array")

    with _TRANSACTION_LOCK:
        return _run_transaction(files, dry_run, return_diff, validate_all, create_plan)


if __name__ == "__main__":
    auth_token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    app = mcp.streamable_http_app()
    if auth_token:
        app.add_middleware(AuthMiddleware, token=auth_token)
        print(f"[auth] token auth enabled ({auth_token[:4]}...{auth_token[-4:]})")
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
