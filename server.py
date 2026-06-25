#!/usr/bin/env python3
"""MCP Streamable HTTP server: full Termux shell access plus file tools."""
import difflib
import os
import pathlib
import re
import subprocess
import unicodedata
import uuid
from collections import OrderedDict
from typing import TypedDict

from mcp.server.fastmcp import FastMCP

READ_MAX_LINES = int(os.environ.get("MCP_READ_MAX_LINES", "2000"))
READ_MAX_BYTES = int(os.environ.get("MCP_READ_MAX_BYTES", str(50 * 1024)))

HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8088"))
TRUNC_LIMIT = int(os.environ.get("MCP_TRUNC_LIMIT", "4096"))
MAX_SESSIONS = int(os.environ.get("MCP_MAX_SESSIONS", "50"))

# session_id -> {"stdout": bytes, "stderr": bytes}
_buffers: "OrderedDict[str, dict]" = OrderedDict()

mcp = FastMCP("termux-shell", host=HOST, port=PORT)


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


class ReadResult(TypedDict):
    data: str | None
    offset: int
    length: int
    total_bytes: int
    eof: bool
    error: str | None


@mcp.tool()
def run_command(command: str, timeout: float | None = None, cwd: str | None = None) -> RunResult:
    """Run a shell command via /bin/sh -c and return stdout/stderr/exit_code.

    Long output is truncated to MCP_TRUNC_LIMIT bytes; full output is kept in a
    buffer readable via read_output using the returned session_id.
    """
    try:
        proc = subprocess.Popen(
            command, shell=True, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            executable="/bin/sh",
        )
    except Exception as e:
        return {"error": f"spawn failed: {e}", "exit_code": None, "timed_out": False,
                "stdout": "", "stderr": "", "stdout_truncated": False, "stderr_truncated": False,
                "stdout_total_bytes": 0, "stderr_total_bytes": 0, "session_id": None}

    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        timed_out = True

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


def _apply_edits(normalized: str, edits: list[dict], path: str) -> tuple[str, str]:
    norm = [{"old": _normalize_lf(e["old_text"]), "new": _normalize_lf(e["new_text"])} for e in edits]
    for i, e in enumerate(norm):
        if not e["old"]:
            raise ValueError(f"edits[{i}].old_text must not be empty in {path}.")
    used_fuzzy = any(_find(normalized, e["old"])[3] for e in norm)
    base = _fuzzy(normalized) if used_fuzzy else normalized
    matched = []
    for i, e in enumerate(norm):
        found, idx, mlen, _ = _find(base, e["old"])
        if not found:
            raise ValueError(f"Could not find edits[{i}] in {path}. old_text must match exactly "
                             f"(including whitespace/newlines).")
        fo = _fuzzy(e["old"]) if used_fuzzy else e["old"]
        occ = base.count(fo)
        if occ > 1:
            raise ValueError(f"Found {occ} occurrences of edits[{i}] in {path}. Each old_text must "
                             f"be unique. Provide more context.")
        matched.append({"i": i, "idx": idx, "len": mlen, "new": e["new"]})
    matched.sort(key=lambda m: m["idx"])
    for a, b in zip(matched, matched[1:]):
        if a["idx"] + a["len"] > b["idx"]:
            raise ValueError(f"edits[{a['i']}] and edits[{b['i']}] overlap in {path}. Merge them.")
    new = base
    for m in reversed(matched):
        new = new[:m["idx"]] + m["new"] + new[m["idx"] + m["len"]:]
    if base == new:
        raise ValueError(f"No changes made to {path}. Replacement produced identical content.")
    return base, new


class WriteResult(TypedDict):
    ok: bool
    path: str
    bytes_written: int
    error: str | None


class EditOp(TypedDict):
    old_text: str
    new_text: str


class EditResult(TypedDict):
    ok: bool
    path: str
    replacements: int
    diff: str | None
    error: str | None


class ReadResult2(TypedDict):
    content: str
    start_line: int
    end_line: int
    total_lines: int
    truncated: bool
    next_offset: int | None
    error: str | None


@mcp.tool()
def write_file(path: str, content: str) -> WriteResult:
    """Create or overwrite a file with content. Parent dirs are created."""
    try:
        p = pathlib.Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode()
        p.write_bytes(data)
        return {"ok": True, "path": str(p), "bytes_written": len(data), "error": None}
    except Exception as e:
        return {"ok": False, "path": path, "bytes_written": 0, "error": str(e)}


@mcp.tool()
def edit_file(path: str, edits: list[EditOp]) -> EditResult:
    """Apply one or more exact-text replacements to a file.

    Each edits[].old_text must match exactly (whitespace/newlines included) and be
    unique in the file. All edits are matched against the original content, must not
    overlap, and are applied atomically (all-or-nothing). If exact match fails, a
    fuzzy match is tried (trailing whitespace, smart quotes, unicode dashes/spaces).
    Returns a line-numbered unified diff of the change.
    """
    if not edits:
        return {"ok": False, "path": path, "replacements": 0, "diff": None,
                "error": "edits must contain at least one replacement."}
    try:
        p = pathlib.Path(path).expanduser()
        raw = p.read_bytes().decode("utf-8")
    except Exception as e:
        return {"ok": False, "path": path, "replacements": 0, "diff": None, "error": str(e)}
    bom, text = _strip_bom(raw)
    ending = _detect_ending(text)
    normalized = _normalize_lf(text)
    try:
        base, new = _apply_edits(normalized, edits, path)
    except ValueError as e:
        return {"ok": False, "path": str(p), "replacements": 0, "diff": None, "error": str(e)}
    try:
        p.write_bytes((bom + _restore_ending(new, ending)).encode("utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(p), "replacements": 0, "diff": None, "error": str(e)}
    diff = "".join(difflib.unified_diff(
        base.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=path, tofile=path, n=3))
    return {"ok": True, "path": str(p), "replacements": len(edits), "diff": diff, "error": None}


@mcp.tool()
def read_file(path: str, offset: int = 1, limit: int | None = None) -> ReadResult2:
    """Read a text file with 1-indexed line numbers (cat -n style).

    offset = 1-indexed line to start from. limit = max lines (default: until the
    2000-line / 50KB cap). Use next_offset from the result to continue large files.
    """
    try:
        p = pathlib.Path(path).expanduser()
        text = p.read_text(errors="replace")
    except Exception as e:
        return {"content": "", "start_line": offset, "end_line": 0, "total_lines": 0,
                "truncated": False, "next_offset": None, "error": str(e)}
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    start = max(0, offset - 1)
    if start >= total:
        return {"content": "", "start_line": offset, "end_line": 0, "total_lines": total,
                "truncated": False, "next_offset": None,
                "error": f"offset {offset} is beyond end of file ({total} lines total)"}
    end = min(start + limit, total) if limit is not None else total
    end = min(end, start + READ_MAX_LINES)
    out, size, n = [], 0, 0
    for idx in range(start, end):
        row = f"{idx + 1:6d}  {lines[idx]}"
        size += len(row.encode()) + 1
        if n > 0 and size > READ_MAX_BYTES:
            end = start + n
            break
        out.append(row)
        n += 1
    truncated = end < total
    next_off = end + 1 if truncated else None
    return {"content": "\n".join(out), "start_line": start + 1, "end_line": start + n,
            "total_lines": total, "truncated": truncated, "next_offset": next_off, "error": None}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
