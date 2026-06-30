#!/usr/bin/env python3
"""MCP Streamable HTTP server: full Termux shell access plus file tools."""
import difflib
import json
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
TRUNC_LIMIT = int(os.environ.get("MCP_TRUNC_LIMIT", "8192"))
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


def _apply_edits(normalized: str, edits: list[dict], path: str,
                 partial: bool = False) -> tuple[str, str, list[dict]]:
    """Match and apply edits. Returns (base, new_content, per_edit_results).
    partial=False -> any failure raises (atomic). partial=True -> skip failures, report them."""
    norm = [{"old": _normalize_lf(e["old_text"]), "new": _normalize_lf(e["new_text"])} for e in edits]
    used_fuzzy = any(_find(normalized, e["old"])[3] for e in norm if e["old"])
    base = _fuzzy(normalized) if used_fuzzy else normalized
    label = lambda i: f"edits[{i}]" if len(norm) > 1 else "the text"
    matched, results = [], []
    for i, e in enumerate(norm):
        if not e["old"]:
            if not partial:
                raise ValueError(f"edits[{i}].old_text must not be empty in {path}.")
            results.append({"index": i, "ok": False, "error": "old_text is empty"})
            continue
        try:
            idx, mlen, newtext = _match_one(base, used_fuzzy, e["old"], e["new"], path, label(i))
            matched.append({"i": i, "idx": idx, "len": mlen, "new": newtext})
            results.append({"index": i, "ok": True, "error": None})
        except ValueError as err:
            if not partial:
                raise
            results.append({"index": i, "ok": False, "error": str(err)})
    matched.sort(key=lambda m: m["idx"])
    for a, b in zip(matched, matched[1:]):
        if a["idx"] + a["len"] > b["idx"]:
            msg = f"edits[{a['i']}] and edits[{b['i']}] overlap in {path}. Merge them."
            if not partial:
                raise ValueError(msg)
            for r in results:
                if r["index"] == b["i"]:
                    r["ok"], r["error"] = False, msg
            matched = [m for m in matched if m["i"] != b["i"]]
    new = base
    for m in sorted(matched, key=lambda m: m["idx"], reverse=True):
        new = new[:m["idx"]] + m["new"] + new[m["idx"] + m["len"]:]
    if base == new and not partial:
        raise ValueError(f"No changes made to {path}. Replacement produced identical content.")
    return base, new, results


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
    results: list[dict] | None
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
def edit_file(path: str, edits: "str | list", dry_run: bool = False,
              partial: bool = False) -> EditResult:
    """Apply one or more text replacements to a file.

    edits is a JSON string: [{"old_text": "...", "new_text": "..."}, ...]
    Passing a JSON string instead of a nested object array avoids MCP transport
    serialization issues with long strings containing newlines and backslashes.

    Matching, tried in order per edit:
      1. exact match
      2. trailing-whitespace / smart-quote / unicode dash+space tolerant match
      3. indent-insensitive match (leading whitespace ignored; new_text is
         automatically re-indented to fit the file). Good for editing indented
         Python/YAML blocks where exact leading spaces are hard to reproduce.
    Each old_text must resolve to a unique location. Edits are matched against the
    original content and must not overlap.

    partial=False (default): atomic, any failed edit aborts the whole call and
      writes nothing. partial=True: apply the edits that match, skip the rest, and
      report per-edit status in `results`.
    dry_run=True: do not write; just return the diff that would result.

    Returns a unified diff plus a per-edit `results` list.
    """
    # Parse edits — accept both JSON string (schema-compliant) and list (RikkaHub sends parsed JSON)
    if isinstance(edits, str):
        try:
            edits = json.loads(edits)
        except (json.JSONDecodeError, TypeError) as e:
            return {"ok": False, "path": path, "replacements": 0, "diff": None,
                    "results": None, "error": f"edits is not valid JSON: {e}"}
    if not isinstance(edits, list) or not edits:
        return {"ok": False, "path": path, "replacements": 0, "diff": None,
                "results": None, "error": "edits must be a non-empty JSON array of {old_text, new_text} objects."}
    try:
        p = pathlib.Path(path).expanduser()
        raw = p.read_bytes().decode("utf-8")
    except Exception as e:
        return {"ok": False, "path": path, "replacements": 0, "diff": None,
                "results": None, "error": str(e)}
    bom, text = _strip_bom(raw)
    ending = _detect_ending(text)
    normalized = _normalize_lf(text)
    try:
        base, new, results = _apply_edits(normalized, edits, path, partial=partial)
    except ValueError as e:
        return {"ok": False, "path": str(p), "replacements": 0, "diff": None,
                "results": None, "error": str(e)}
    applied = sum(1 for r in results if r["ok"])
    diff = "".join(difflib.unified_diff(
        base.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=path, tofile=path, n=3))
    if dry_run:
        return {"ok": True, "path": str(p), "replacements": applied, "diff": diff,
                "results": results, "error": None}
    if base == new:
        return {"ok": False, "path": str(p), "replacements": 0, "diff": "",
                "results": results, "error": "No edits matched; nothing written."}
    try:
        p.write_bytes((bom + _restore_ending(new, ending)).encode("utf-8"))
    except Exception as e:
        return {"ok": False, "path": str(p), "replacements": 0, "diff": None,
                "results": results, "error": str(e)}
    return {"ok": True, "path": str(p), "replacements": applied, "diff": diff,
            "results": results, "error": None}


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
    body = "\n".join(out)
    if truncated:
        remaining = total - (start + n)
        body += (f"\n\n--- TRUNCATED: showing lines {start + 1}-{start + n} of {total} "
                 f"({remaining} more). Use offset={next_off} to continue. ---")
    return {"content": body, "start_line": start + 1, "end_line": start + n,
            "total_lines": total, "truncated": truncated, "next_offset": next_off, "error": None}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
