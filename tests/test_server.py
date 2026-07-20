import asyncio
import base64
import hashlib
import os
import pathlib
import shlex
import time
import threading

import server


# ---------------------------------------------------------------------------
# Existing baseline tests (preserved)
# ---------------------------------------------------------------------------

def test_edit_noop_is_success(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("same\n")
    result = server.edit_file(str(path), [{"mode": "replace_match", "match_text": "same", "write_text": "same"}])
    assert result["ok"] is True
    assert result["changed"] is False
    assert result["replacements"] == 0
    assert result["results"][0]["status"] == "matched_no_change"
    assert path.read_text() == "same\n"


def test_atomic_failure_has_per_edit_diagnostics_and_does_not_write(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("one\ntwo\n")
    result = server.edit_file(str(path), [
        {"mode": "replace_match", "match_text": "one", "write_text": "ONE"},
        {"mode": "replace_match", "match_text": "missing", "write_text": "MISSING"},
    ])
    assert result["ok"] is False
    assert result["batch_aborted"] is True
    assert result["changed"] is False
    assert [r["index"] for r in result["results"]] == [0, 1]
    assert result["results"][0]["matched"] is True
    assert result["results"][0]["status"] == "aborted"
    assert result["results"][1]["matched"] is False
    assert result["results"][1]["reason"]
    assert path.read_text() == "one\ntwo\n"


def test_append_is_atomic_creates_parent_and_reports_metadata(tmp_path):
    path = tmp_path / "nested" / "file.txt"
    result = server.append_file(str(path), "hello")
    assert result["ok"] is True
    assert result["bytes_written"] == 5
    assert result["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert path.read_text() == "hello"
    current_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    guarded = server.append_file(str(path), "!", expected_sha256=current_sha)
    assert guarded["ok"] is True
    assert path.read_text() == "hello!"
    failed = server.append_file(str(path), "?", expected_sha256="0" * 64)
    assert failed["ok"] is False
    assert path.read_text() == "hello!"


def test_read_file_bytes_paginates_binary(tmp_path):
    path = tmp_path / "data.bin"
    path.write_bytes(bytes(range(10)))
    result = server.read_file_bytes(str(path), offset=3, length=4)
    assert result["ok"] is True
    assert result["offset"] == 3
    assert result["length"] == 4
    assert base64.b64decode(result["data_base64"]) == bytes(range(3, 7))
    assert result["eof"] is False


def test_write_file_sha256_and_atomic_payload(tmp_path):
    path = tmp_path / "large.txt"
    payload = "x" * 10000
    result = server.write_file(str(path), payload)
    assert result["ok"] is True
    assert result["bytes_written"] == len(payload.encode())
    assert path.read_text() == payload


def test_run_command_truncation_has_next_offsets(monkeypatch):
    monkeypatch.setattr(server, "TRUNC_LIMIT", 3)
    result = asyncio.run(server.run_command("printf 123456; printf abcdef >&2"))
    assert result["stdout_truncated"] is True
    assert result["stderr_truncated"] is True
    assert result["stdout_next_offset"] == 3
    assert result["stderr_next_offset"] == 3
    assert result["session_id"]
    assert server.read_output(result["session_id"], "stdout", 3, 3)["data"] == "456"


def test_long_run_command_does_not_block_concurrent_read_file(tmp_path):
    path = tmp_path / "ready.txt"
    path.write_text("ready\n")

    async def scenario():
        command_task = asyncio.create_task(
            server.mcp.call_tool("run_command", {"command": "sleep 0.5"}))
        await asyncio.sleep(0.05)
        loop = asyncio.get_running_loop()
        started = loop.time()
        _, read_result = await asyncio.wait_for(
            server.mcp.call_tool("read_file", {"path": str(path)}), timeout=0.2)
        elapsed = loop.time() - started
        assert not command_task.done()
        _, command_result = await command_task
        return read_result, elapsed, command_result

    read_result, elapsed, command_result = asyncio.run(scenario())
    assert read_result["error"] is None
    assert "ready" in read_result["content"]
    assert elapsed < 0.2
    assert command_result["exit_code"] == 0


def test_run_command_timeout_kills_process_group(tmp_path):
    side_effect = tmp_path / "timeout-leak.txt"
    command = f"(sleep 0.4; printf leaked > {shlex.quote(str(side_effect))}) & wait"

    async def scenario():
        result = await server.run_command(command, timeout=0.05)
        await asyncio.sleep(0.5)
        return result

    result = asyncio.run(scenario())
    assert result["timed_out"] is True
    assert result["exit_code"] is not None
    assert not side_effect.exists()


def test_run_command_cancellation_kills_process_group(tmp_path):
    side_effect = tmp_path / "cancel-leak.txt"
    command = f"(sleep 0.4; printf leaked > {shlex.quote(str(side_effect))}) & wait"

    async def scenario():
        task = asyncio.create_task(server.run_command(command))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("run_command did not propagate cancellation")
        await asyncio.sleep(0.5)

    asyncio.run(scenario())
    assert not side_effect.exists()


# ---------------------------------------------------------------------------
# read_file SHA-256
# ---------------------------------------------------------------------------

def test_read_file_exposes_sha256(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"hello\nworld\n")
    result = server.read_file(str(path))
    assert result["sha256"] == hashlib.sha256(b"hello\nworld\n").hexdigest()
    assert "hello" in result["content"]


def test_read_file_sha256_none_on_error(tmp_path):
    result = server.read_file(str(tmp_path / "nonexistent.txt"))
    assert result["sha256"] is None
    assert result["error"]


def test_read_file_raw_preserves_exact_selected_text(tmp_path):
    path = tmp_path / "raw.txt"
    path.write_bytes(b"alpha\r\n  beta\r\ngamma")

    result = server.read_file(str(path), offset=1, limit=2, line_numbers=False)

    assert result["content"] == "alpha\r\n  beta\r\n"
    assert result["truncated"] is True
    assert result["next_offset"] == 3
    assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_read_files_batches_ranges_and_item_errors(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one\ntwo\n")
    second.write_text("three\nfour\n")

    result = server.read_files([
        {"path": str(first), "limit": 1, "line_numbers": False},
        {"path": str(second), "offset": 2},
        {"path": str(tmp_path / "missing.txt")},
    ])

    assert result["error"] is None
    assert result["results"][0]["content"] == "one\n"
    assert "four" in result["results"][1]["content"]
    assert result["results"][2]["error"]
    assert [item["path"] for item in result["results"]] == [
        str(first), str(second), str(tmp_path / "missing.txt")]


def test_filesystem_tools_are_registered_async():
    names = ("write_file", "append_file", "edit_file", "read_file_bytes",
             "read_file", "read_files", "edit_files")
    assert all(server.mcp._tool_manager._tools[name].is_async for name in names)


def test_slow_read_file_does_not_block_event_loop(tmp_path, monkeypatch):
    path = tmp_path / "slow.txt"
    path.write_text("ready\n")
    original = pathlib.Path.read_bytes

    def slow_read_bytes(self):
        if self == path:
            time.sleep(0.3)
        return original(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", slow_read_bytes)

    async def scenario():
        loop = asyncio.get_running_loop()
        started = loop.time()
        task = asyncio.create_task(
            server.mcp.call_tool("read_file", {"path": str(path)}))
        await asyncio.sleep(0.05)
        elapsed = loop.time() - started
        assert not task.done()
        _, result = await task
        return elapsed, result

    elapsed, result = asyncio.run(scenario())
    assert elapsed < 0.2
    assert "ready" in result["content"]


# ---------------------------------------------------------------------------
# edit_file expected_sha256 stale protection
# ---------------------------------------------------------------------------

def test_edit_file_expected_sha256_success(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("alpha\n")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    result = server.edit_file(str(path), [{"mode": "replace_match", "match_text": "alpha", "write_text": "beta"}],
                              expected_sha256=sha)
    assert result["ok"] is True
    assert path.read_text() == "beta\n"


def test_edit_file_expected_sha256_stale(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("alpha\n")
    result = server.edit_file(str(path), [{"mode": "replace_match", "match_text": "alpha", "write_text": "beta"}],
                              expected_sha256="0" * 64)
    assert result["ok"] is False
    assert "stale" in result["error"].lower()
    assert path.read_text() == "alpha\n"


# ---------------------------------------------------------------------------
# Explicit edit modes
# ---------------------------------------------------------------------------

def test_replace_match_mode(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("foo\nbar\nbaz\n")
    result = server.edit_file(str(path), [
        {"mode": "replace_match", "match_text": "bar", "write_text": "BAR"}
    ])
    assert result["ok"]
    assert path.read_text() == "foo\nBAR\nbaz\n"
    assert result["results"][0]["mode"] == "replace_match"


def test_insert_before_mode(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("a\nb\nc\n")
    result = server.edit_file(str(path), [
        {"mode": "insert_before", "match_text": "b", "write_text": "INSERTED"}
    ])
    assert result["ok"]
    assert path.read_text() == "a\nINSERTEDb\nc\n"


def test_insert_after_mode(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("a\nb\nc\n")
    result = server.edit_file(str(path), [
        {"mode": "insert_after", "match_text": "b", "write_text": "INSERTED"}
    ])
    assert result["ok"]
    assert path.read_text() == "a\nbINSERTED\nc\n"


def test_insert_before_with_newline_content(tmp_path):
    """Literal newline in content is preserved; no extra newline is added."""
    path = tmp_path / "f.txt"
    path.write_text("a\nb\nc\n")
    result = server.edit_file(str(path), [
        {"mode": "insert_before", "match_text": "b", "write_text": "X\nY\n"}
    ])
    assert result["ok"]
    assert path.read_text() == "a\nX\nY\nb\nc\n"


def test_insert_after_with_newline_content(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("a\nb\nc\n")
    result = server.edit_file(str(path), [
        {"mode": "insert_after", "match_text": "b", "write_text": "\nX\nY"}
    ])
    assert result["ok"]
    assert path.read_text() == "a\nb\nX\nY\nc\n"


def test_same_boundary_edits_abort_without_writing(tmp_path):
    path = tmp_path / "imports.py"
    source = "import re\nimport subprocess\nimport tempfile\n"
    edits = [
        {"mode": "insert_after", "match_text": "import re\n",
         "write_text": "import signal\n"},
        {"mode": "replace_match", "match_text": "import subprocess\n",
         "write_text": ""},
    ]

    for payload in (edits, list(reversed(edits))):
        path.write_text(source)
        result = server.edit_file(str(path), payload)
        assert not result["ok"]
        assert result["batch_aborted"]
        assert any("conflict" in (item["reason"] or "") for item in result["results"])
        assert path.read_text() == source


def test_fuzzy_match_preserves_all_unmatched_text(tmp_path):
    path = tmp_path / "unicode.txt"
    source = "value = “target”\nkeep = “untouched”—x  \n"
    path.write_text(source)

    result = server.edit_file(str(path), [{
        "mode": "replace_match",
        "match_text": 'value = "target"',
        "write_text": 'value = "changed"',
    }])

    assert result["ok"]
    assert path.read_text() == 'value = "changed"\nkeep = “untouched”—x  \n'


def test_fuzzy_match_maps_nfkc_source_spans(tmp_path):
    ligature = tmp_path / "ligature.txt"
    combining = tmp_path / "combining.txt"
    ligature.write_text("ﬁle = 1\nkeep = “x”—y  \n")
    combining.write_text("cafe\u0301 = 1\nkeep  \n")

    first = server.edit_file(str(ligature), [{
        "mode": "replace_match", "match_text": "file = 1", "write_text": "file = 2",
    }])
    second = server.edit_file(str(combining), [{
        "mode": "replace_match", "match_text": "café = 1", "write_text": "cafe = 2",
    }])

    assert first["ok"] and second["ok"]
    assert ligature.read_text() == "file = 2\nkeep = “x”—y  \n"
    assert combining.read_text() == "cafe = 2\nkeep  \n"


def test_adjacent_and_separate_edits_remain_allowed(tmp_path):
    adjacent = tmp_path / "adjacent.txt"
    imports = tmp_path / "imports.py"
    adjacent.write_text("abc")
    imports.write_text("import re\nimport subprocess\nimport tempfile\n")

    first = server.edit_file(str(adjacent), [
        {"mode": "replace_match", "match_text": "a", "write_text": "A"},
        {"mode": "replace_match", "match_text": "b", "write_text": "B"},
    ])
    second = server.edit_file(str(imports), [
        {"mode": "insert_after", "match_text": "import re\n",
         "write_text": "import signal\n"},
        {"mode": "replace_match", "match_text": "import tempfile",
         "write_text": "import pathlib"},
    ])

    assert first["ok"] and second["ok"]
    assert adjacent.read_text() == "ABc"
    assert imports.read_text() == (
        "import re\nimport signal\nimport subprocess\nimport pathlib\n")




# ---------------------------------------------------------------------------
# Ambiguous / missing anchors and bounded diagnostics
# ---------------------------------------------------------------------------

def test_ambiguous_anchor_rejected(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("dup\ndup\nother\n")
    result = server.edit_file(str(path), [
        {"mode": "insert_before", "match_text": "dup", "write_text": "X"}
    ])
    assert not result["ok"]
    assert result["batch_aborted"]
    r = result["results"][0]
    assert not r["matched"]
    assert r["match_count"] is not None and r["match_count"] >= 2


def test_missing_anchor_has_closest_match(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("hello world\nfoo bar\nbaz qux\n")
    result = server.edit_file(str(path), [
        {"mode": "insert_before", "match_text": "hello wrld", "write_text": "X"}
    ])
    assert not result["ok"]
    r = result["results"][0]
    assert not r["matched"]
    assert "closest_match_line" in r
    assert "similarity" in r
    assert "nearby_text" in r
    assert r["closest_match_line"] == 1


def test_missing_match_text_has_closest_match(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("def foo():\n    return 42\n")
    result = server.edit_file(str(path), [
        {"mode": "replace_match", "match_text": "def foo:", "write_text": "X"}
    ])
    assert not result["ok"]
    r = result["results"][0]
    assert "closest_match_line" in r
    assert r["closest_match_line"] == 1


def test_failed_match_diagnostics_use_real_file_lines_for_both_edit_tools(tmp_path):
    source = b"line one\r\nline two\rliteral\r\n\r\nclosing)\r\n"
    paths = [tmp_path / "single.txt", tmp_path / "multi.txt"]
    for path in paths:
        path.write_bytes(source)

    single = server.edit_file(str(paths[0]), [
        {"mode": "replace_match", "match_text": "closing}", "write_text": "X"}
    ])
    multi = server.edit_files([{
        "path": str(paths[1]),
        "edits": [
            {"mode": "replace_match", "match_text": "closing}", "write_text": "X"}
        ],
    }])

    diagnostics = [single["results"][0], multi["files"][0]["results"][0]]
    for result in diagnostics:
        assert result["closest_match_line"] == 4
        assert ">> 4: closing)" in result["nearby_text"]
        assert "2: line two\\rliteral" in result["nearby_text"]
    assert all(path.read_bytes() == source for path in paths)


def test_diagnostics_are_bounded(tmp_path):
    """nearby_text should never include large unrelated content."""
    path = tmp_path / "f.txt"
    path.write_text("\n".join(f"line {i}" for i in range(1000)) + "\n")
    result = server.edit_file(str(path), [
        {"mode": "replace_match", "match_text": "nonexistent text here", "write_text": "X"}
    ])
    assert not result["ok"]
    r = result["results"][0]
    if "nearby_text" in r:
        assert len(r["nearby_text"]) <= 500


# ---------------------------------------------------------------------------
# edit_files: two-file atomic success
# ---------------------------------------------------------------------------

def test_two_file_atomic_success(tmp_path):
    p1 = tmp_path / "a.txt"
    p2 = tmp_path / "b.txt"
    p1.write_text("alpha\n")
    p2.write_text("beta\n")
    result = server.edit_files([
        {"path": str(p1), "edits": [{"mode": "replace_match", "match_text": "alpha", "write_text": "ALPHA"}]},
        {"path": str(p2), "edits": [{"mode": "replace_match", "match_text": "beta", "write_text": "BETA"}]},
    ])
    assert result["ok"]
    assert result["applied"]
    assert p1.read_text() == "ALPHA\n"
    assert p2.read_text() == "BETA\n"
    assert len(result["files"]) == 2
    for f in result["files"]:
        assert f["ok"]
        assert f["sha256"]
        assert f["result_sha256"]
        assert f["diff"]


# ---------------------------------------------------------------------------
# Failed second file leaves both byte-identical
# ---------------------------------------------------------------------------

def test_failed_second_file_leaves_both_unchanged(tmp_path):
    p1 = tmp_path / "a.txt"
    p2 = tmp_path / "b.txt"
    p1.write_text("alpha\n")
    p2.write_text("beta\n")
    orig1 = p1.read_bytes()
    orig2 = p2.read_bytes()
    result = server.edit_files([
        {"path": str(p1), "edits": [{"mode": "replace_match", "match_text": "alpha", "write_text": "ALPHA"}]},
        {"path": str(p2), "edits": [{"mode": "replace_match", "match_text": "nonexistent", "write_text": "X"}]},
    ])
    assert not result["ok"]
    assert not result["applied"]
    assert p1.read_bytes() == orig1
    assert p2.read_bytes() == orig2


# ---------------------------------------------------------------------------
# Stale hash single and multi
# ---------------------------------------------------------------------------

def test_stale_hash_single_file(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("alpha\n")
    result = server.edit_files([
        {"path": str(p),
         "edits": [{"mode": "replace_match", "match_text": "alpha", "write_text": "BETA"}],
         "expected_sha256": "0" * 64},
    ])
    assert not result["ok"]
    assert "stale" in result["error"].lower()
    assert p.read_text() == "alpha\n"


def test_stale_hash_multi_file(tmp_path):
    p1 = tmp_path / "a.txt"
    p2 = tmp_path / "b.txt"
    p1.write_text("alpha\n")
    p2.write_text("beta\n")
    sha1 = hashlib.sha256(p1.read_bytes()).hexdigest()
    result = server.edit_files([
        {"path": str(p1), "edits": [{"mode": "replace_match", "match_text": "alpha", "write_text": "X"}],
         "expected_sha256": sha1},
        {"path": str(p2), "edits": [{"mode": "replace_match", "match_text": "beta", "write_text": "Y"}],
         "expected_sha256": "0" * 64},
    ])
    assert not result["ok"]
    assert "stale" in result["error"].lower()
    assert p1.read_text() == "alpha\n"
    assert p2.read_text() == "beta\n"


# ---------------------------------------------------------------------------
# Duplicate canonical / symlink aliases
# ---------------------------------------------------------------------------

def test_duplicate_path_rejected(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("alpha\n")
    result = server.edit_files([
        {"path": str(p), "edits": [{"mode": "replace_match", "match_text": "alpha", "write_text": "X"}]},
        {"path": str(p), "edits": [{"mode": "replace_match", "match_text": "alpha", "write_text": "Y"}]},
    ])
    assert not result["ok"]
    assert "duplicate" in result["error"].lower()


def test_symlink_alias_rejected(tmp_path):
    target = tmp_path / "real.txt"
    target.write_text("data\n")
    link = tmp_path / "link.txt"
    try:
        os.symlink(target, link)
    except OSError:
        return  # symlinks not supported on this platform
    result = server.edit_files([
        {"path": str(target), "edits": [{"mode": "replace_match", "match_text": "data", "write_text": "X"}]},
        {"path": str(link), "edits": [{"mode": "replace_match", "match_text": "data", "write_text": "Y"}]},
    ])
    assert not result["ok"]
    assert "duplicate" in result["error"].lower()


# ---------------------------------------------------------------------------
# Dry-run no-write and per-file diff
# ---------------------------------------------------------------------------

def test_dry_run_no_write(tmp_path):
    p1 = tmp_path / "a.txt"
    p2 = tmp_path / "b.txt"
    p1.write_text("alpha\n")
    p2.write_text("beta\n")
    orig1 = p1.read_bytes()
    orig2 = p2.read_bytes()
    result = server.edit_files([
        {"path": str(p1), "edits": [{"mode": "replace_match", "match_text": "alpha", "write_text": "ALPHA"}]},
        {"path": str(p2), "edits": [{"mode": "replace_match", "match_text": "beta", "write_text": "BETA"}]},
    ], dry_run=True)
    assert result["ok"]
    assert result["dry_run"]
    assert not result["applied"]
    assert p1.read_bytes() == orig1
    assert p2.read_bytes() == orig2


def test_dry_run_per_file_diff(tmp_path):
    p1 = tmp_path / "a.txt"
    p2 = tmp_path / "b.txt"
    p1.write_text("alpha\n")
    p2.write_text("beta\n")
    result = server.edit_files([
        {"path": str(p1), "edits": [{"mode": "replace_match", "match_text": "alpha", "write_text": "ALPHA"}]},
        {"path": str(p2), "edits": [{"mode": "replace_match", "match_text": "beta", "write_text": "BETA"}]},
    ], dry_run=True)
    assert result["ok"]
    diffs = [f["diff"] for f in result["files"]]
    assert all(d for d in diffs)
    assert "ALPHA" in diffs[0]
    assert "BETA" in diffs[1]


def test_dry_run_source_and_result_sha256(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("alpha\n")
    result = server.edit_files([
        {"path": str(p), "edits": [{"mode": "replace_match", "match_text": "alpha", "write_text": "beta"}]},
    ], dry_run=True)
    assert result["ok"]
    f = result["files"][0]
    assert f["sha256"] == hashlib.sha256(b"alpha\n").hexdigest()
    assert f["result_sha256"] == hashlib.sha256(b"beta\n").hexdigest()




# ---------------------------------------------------------------------------
# BOM / CRLF / mode preservation
# ---------------------------------------------------------------------------

def test_bom_crlf_mode_preservation_in_edit_files(tmp_path):
    p = tmp_path / "f.txt"
    content = "\ufeffline1\r\nline2\r\n"
    p.write_bytes(content.encode("utf-8"))
    original_mode = p.stat().st_mode & 0o777
    result = server.edit_files([
        {"path": str(p), "edits": [{"mode": "replace_match", "match_text": "line1", "write_text": "LINE1"}]},
    ])
    assert result["ok"]
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM preserved
    assert b"\r\n" in raw  # CRLF preserved
    assert (p.stat().st_mode & 0o777) == original_mode  # mode preserved
    assert b"LINE1" in raw


def test_bom_crlf_preservation_in_edit_file(tmp_path):
    p = tmp_path / "f.txt"
    content = "\ufeffhello\r\nworld\r\n"
    p.write_bytes(content.encode("utf-8"))
    original_mode = p.stat().st_mode & 0o777
    result = server.edit_file(str(p), [
        {"mode": "replace_match", "match_text": "hello", "write_text": "HELLO"},
    ])
    assert result["ok"]
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in raw
    assert (p.stat().st_mode & 0o777) == original_mode


# ---------------------------------------------------------------------------
# Rollback simulation
# ---------------------------------------------------------------------------

def test_rollback_on_publication_failure(tmp_path, monkeypatch):
    p1 = tmp_path / "f1.txt"
    p2 = tmp_path / "f2.txt"
    p1.write_text("one\n")
    p2.write_text("two\n")
    orig1 = p1.read_bytes()
    orig2 = p2.read_bytes()

    original_replace = os.replace
    call_count = [0]

    def failing_replace(src, dst):
        call_count[0] += 1
        if call_count[0] == 2:
            raise OSError("injected I/O failure")
        return original_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing_replace)
    result = server.edit_files([
        {"path": str(p1), "edits": [{"mode": "replace_match", "match_text": "one", "write_text": "ONE"}]},
        {"path": str(p2), "edits": [{"mode": "replace_match", "match_text": "two", "write_text": "TWO"}]},
    ])
    monkeypatch.undo()

    assert not result["ok"]
    assert result["rollback"] is not None
    assert len(result["rollback"]) == 1  # first file was rolled back
    assert result["rollback"][0]["restored"] is True
    assert p1.read_bytes() == orig1  # rolled back to original
    assert p2.read_bytes() == orig2  # never written


# ---------------------------------------------------------------------------
# Concurrency / stale protection
# ---------------------------------------------------------------------------

def test_concurrent_stale_protection(tmp_path):
    """Two concurrent edits with the same expected_sha256: one succeeds, one stale."""
    p = tmp_path / "f.txt"
    p.write_text("a\nb\nc\n")
    sha = hashlib.sha256(p.read_bytes()).hexdigest()

    barrier = threading.Barrier(2)
    results = [None, None]

    def edit_thread(idx):
        barrier.wait()
        results[idx] = server.edit_files([
            {"path": str(p),
             "edits": [{"mode": "replace_match", "match_text": "b", "write_text": f"v{idx}"}],
             "expected_sha256": sha},
        ])

    t1 = threading.Thread(target=edit_thread, args=(0,))
    t2 = threading.Thread(target=edit_thread, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    successes = [r for r in results if r and r["ok"]]
    failures = [r for r in results if r and not r["ok"]]
    assert len(successes) == 1
    assert len(failures) == 1
    assert "stale" in failures[0]["error"].lower()


# ---------------------------------------------------------------------------
# Minimal public edit contracts
# ---------------------------------------------------------------------------

def test_edit_apis_reject_legacy_payloads(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("alpha\n")
    legacy_edit = [{"old_text": "alpha", "new_text": "beta"}]
    assert not server.edit_file(str(path), legacy_edit)["ok"]
    assert not server.edit_file(str(path), "[]")["ok"]
    assert not server.edit_files("[]")["ok"]
    assert path.read_text() == "alpha\n"


def test_tmp_alias_works_across_file_tools(tmp_path, monkeypatch):
    temp_root = tmp_path / "termux-tmp"
    temp_root.mkdir()
    monkeypatch.setattr(server, "TEMP_ROOT", temp_root)

    written = server.write_file("/tmp/nested/file.txt", "alpha\n")
    actual = temp_root / "nested" / "file.txt"
    assert written["ok"] and written["path"] == str(actual)
    assert actual.read_text() == "alpha\n"

    read = server.read_file("/tmp/nested/file.txt", line_numbers=False)
    assert read["path"] == str(actual)
    assert read["content"] == "alpha\n"

    appended = server.append_file(
        "/tmp/nested/file.txt", "beta\n", expected_sha256=read["sha256"])
    assert appended["ok"] and appended["path"] == str(actual)

    edited = server.edit_file("/tmp/nested/file.txt", [{
        "mode": "replace_match",
        "match_text": "beta",
        "write_text": "BETA",
    }])
    assert edited["ok"] and edited["path"] == str(actual)

    binary = server.read_file_bytes("/tmp/nested/file.txt")
    assert binary["ok"] and binary["path"] == str(actual)
    assert base64.b64decode(binary["data_base64"]) == b"alpha\nBETA\n"


def test_tmp_alias_works_for_multi_edit_and_command_cwd(tmp_path, monkeypatch):
    temp_root = tmp_path / "termux-tmp"
    work = temp_root / "work"
    work.mkdir(parents=True)
    (work / "a.txt").write_text("alpha\n")
    monkeypatch.setattr(server, "TEMP_ROOT", temp_root)

    edited = server.edit_files([{
        "path": "/tmp/work/a.txt",
        "edits": [{
            "mode": "replace_match",
            "match_text": "alpha",
            "write_text": "beta",
        }],
    }])
    assert edited["ok"]
    assert edited["files"][0]["path"] == str(work / "a.txt")

    command = asyncio.run(server.run_command(
        "pwd; printf done > relative.txt", cwd="/tmp/work"))
    assert command["exit_code"] == 0
    assert command["stdout"].strip() == str(work)
    assert (work / "relative.txt").read_text() == "done"


def test_tmp_alias_rejects_escape(tmp_path, monkeypatch):
    temp_root = tmp_path / "termux-tmp"
    temp_root.mkdir()
    monkeypatch.setattr(server, "TEMP_ROOT", temp_root)

    result = server.write_file("/tmp/../escape.txt", "nope")
    assert not result["ok"]
    assert "escapes" in result["error"]
    assert not (tmp_path / "escape.txt").exists()


def test_public_tool_signatures_are_minimal():
    import inspect
    assert list(inspect.signature(server.write_file).parameters) == [
        "path", "content", "expected_sha256", "create_only"]
    assert list(inspect.signature(server.append_file).parameters) == [
        "path", "content", "expected_sha256"]
    assert list(inspect.signature(server.edit_file).parameters) == [
        "path", "edits", "dry_run", "expected_sha256"]
    assert list(inspect.signature(server.edit_files).parameters) == ["files", "dry_run"]


# ---------------------------------------------------------------------------
# edit_file with explicit modes and dry_run
# ---------------------------------------------------------------------------

def test_edit_file_dry_run_no_write(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("alpha\n")
    orig = p.read_bytes()
    result = server.edit_file(str(p), [
        {"mode": "replace_match", "match_text": "alpha", "write_text": "beta"},
    ], dry_run=True)
    assert result["ok"]
    assert result["diff"]
    assert result["dry_run"] is True
    assert result["applied"] is False
    assert p.read_bytes() == orig


# ---------------------------------------------------------------------------
# Binary / non-UTF-8 rejection
# ---------------------------------------------------------------------------

def test_edit_files_rejects_binary(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"\xff\xfe\x00\x01binary\xff")
    result = server.edit_files([
        {"path": str(p), "edits": [{"mode": "replace_match", "match_text": "x", "write_text": "y"}]},
    ])
    assert not result["ok"]
    assert "utf-8" in result["error"].lower() or "unicode" in result["error"].lower()


# ---------------------------------------------------------------------------
# Contract schemas, compatibility aliases, paths, write guards, safe defaults
# ---------------------------------------------------------------------------

def _schema_ref(schema, node):
    ref = node.get("$ref")
    return schema["$defs"][ref.rsplit("/", 1)[-1]] if ref else node


def test_generated_mcp_schemas_expose_nested_contracts():
    edit_schema = server.mcp._tool_manager._tools["edit_file"].parameters
    edit_item = _schema_ref(edit_schema, edit_schema["properties"]["edits"]["items"])
    assert set(edit_item["required"]) == {"mode", "match_text", "write_text"}
    assert edit_item["properties"]["mode"]["enum"] == [
        "replace_match", "insert_before", "insert_after"]
    assert edit_item["properties"]["match_text"]["minLength"] == 1
    assert "matchText" in edit_item["properties"]["match_text"]["description"]
    assert "writeText" in edit_item["properties"]["write_text"]["description"]
    assert "insert_before_match" in edit_item["properties"]["mode"]["description"]

    files_schema = server.mcp._tool_manager._tools["edit_files"].parameters
    file_item = _schema_ref(files_schema, files_schema["properties"]["files"]["items"])
    assert set(file_item["required"]) == {"path", "edits"}
    nested_edit = _schema_ref(files_schema, file_item["properties"]["edits"]["items"])
    assert nested_edit["properties"]["mode"]["enum"] == [
        "replace_match", "insert_before", "insert_after"]

    reads_schema = server.mcp._tool_manager._tools["read_files"].parameters
    read_item = _schema_ref(reads_schema, reads_schema["properties"]["reads"]["items"])
    assert read_item["required"] == ["path"]
    assert set(read_item["properties"]) == {
        "path", "offset", "limit", "line_numbers"}


def test_mcp_alias_payloads_normalize_results(tmp_path):
    path = tmp_path / "alias.txt"
    path.write_text("alpha\nbeta\n")

    async def scenario():
        _, result = await server.mcp.call_tool("edit_file", {
            "path": str(path),
            "edits": [{
                "mode": "insert_after_match",
                "matchText": "alpha",
                "writeText": "\ninserted",
            }],
        })
        return result

    result = asyncio.run(scenario())
    assert result["ok"]
    assert result["results"][0]["mode"] == "insert_after"
    assert path.read_text() == "alpha\ninserted\nbeta\n"


def test_edit_aliases_and_conflicts_direct_runtime(tmp_path):
    path = tmp_path / "alias.txt"
    path.write_text("alpha\n")
    accepted = server.edit_file(str(path), [{
        "mode": "insert_before_match", "matchText": "alpha", "writeText": "x\n"}])
    assert accepted["ok"]
    assert accepted["results"][0]["mode"] == "insert_before"

    equal_duplicates = server.edit_file(str(path), [{
        "mode": "replace_match", "match_text": "alpha", "matchText": "alpha",
        "write_text": "alpha", "writeText": "alpha"}])
    assert equal_duplicates["ok"]

    conflict = server.edit_file(str(path), [{
        "mode": "replace_match", "match_text": "alpha", "matchText": "other",
        "write_text": "beta", "writeText": "beta"}])
    assert not conflict["ok"]
    assert "conflicting values for match_text and matchText" in conflict["error"]

    unknown = server.edit_file(str(path), [{
        "mode": "replace_match", "match_text": "alpha", "write_text": "beta",
        "unexpected": True}])
    assert not unknown["ok"]
    assert "unsupported fields ['unexpected']" in unknown["error"]


def test_edit_validation_errors_explain_recovery(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("alpha\n")
    payloads = [
        [{"old_text": "alpha", "new_text": "beta"}],
        [{"mode": "replace", "match_text": "alpha", "write_text": "beta"}],
        [{"mode": "replace_match", "match_text": "alpha"}],
    ]
    for payload in payloads:
        result = server.edit_file(str(path), payload)
        assert not result["ok"]
        assert "match_text (or matchText)" in result["error"]
        assert "write_text (or writeText)" in result["error"]
        assert "replace_match, insert_before, insert_after" in result["error"]
        assert "insert_before_match, insert_after_match" in result["error"]
    assert "unsupported" in server.edit_file(str(path), payloads[0])["error"]
    assert "invalid mode" in server.edit_file(str(path), payloads[1])["error"]
    assert path.read_text() == "alpha\n"


def test_edit_files_content_entry_points_to_write_file(tmp_path):
    path = tmp_path / "missing.txt"
    result = server.edit_files([{"path": str(path), "content": "new"}])
    assert not result["ok"]
    assert "existing UTF-8 files" in result["error"]
    assert "write_file" in result["error"]
    assert not path.exists()


def test_relative_file_paths_resolve_from_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    elsewhere = tmp_path / "elsewhere"
    home.mkdir()
    elsewhere.mkdir()
    monkeypatch.setattr(server, "HOME_ROOT", home)
    monkeypatch.chdir(elsewhere)

    written = server.write_file("project/file.txt", "alpha\n")
    assert written["ok"]
    assert written["path"] == str(home / "project" / "file.txt")
    assert not (elsewhere / "project" / "file.txt").exists()
    read = server.read_file("project/file.txt", line_numbers=False)
    assert read["content"] == "alpha\n"


def test_run_command_default_and_relative_cwd_use_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    child = home / "project"
    child.mkdir(parents=True)
    monkeypatch.setattr(server, "HOME_ROOT", home)

    default = asyncio.run(server.run_command("pwd; printf /tmp/literal"))
    relative = asyncio.run(server.run_command("pwd", cwd="project"))
    assert default["exit_code"] == 0
    lines = default["stdout"].splitlines()
    assert lines[0] == str(home)
    assert lines[1] == "/tmp/literal"
    assert relative["stdout"].strip() == str(child)


def test_write_file_guards(tmp_path):
    path = tmp_path / "guarded.txt"
    created = server.write_file(str(path), "one", create_only=True)
    assert created["ok"]
    blocked_create = server.write_file(str(path), "two", create_only=True)
    assert not blocked_create["ok"]
    assert path.read_text() == "one"

    stale = server.write_file(str(path), "two", expected_sha256="0" * 64)
    assert not stale["ok"]
    assert "stale" in stale["error"]
    assert path.read_text() == "one"

    source_sha = hashlib.sha256(b"one").hexdigest().upper()
    replaced = server.write_file(str(path), "two", expected_sha256=source_sha)
    assert replaced["ok"]
    assert path.read_text() == "two"

    missing = server.write_file(
        str(tmp_path / "missing.txt"), "x", expected_sha256=source_sha)
    assert not missing["ok"]
    assert "does not exist" in missing["error"]
    assert not (tmp_path / "missing.txt").exists()


def test_write_file_create_only_rejects_dangling_symlink(tmp_path):
    target = tmp_path / "missing-target.txt"
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    result = server.write_file(str(link), "replacement", create_only=True)
    assert not result["ok"]
    assert "already exists" in result["error"]
    assert link.is_symlink()
    assert not target.exists()


def test_write_file_concurrent_expected_hash_serializes(tmp_path):
    path = tmp_path / "race.txt"
    path.write_text("base")
    sha = hashlib.sha256(b"base").hexdigest()
    barrier = threading.Barrier(2)
    results = []

    def writer(value):
        barrier.wait()
        results.append(server.write_file(
            str(path), value, expected_sha256=sha))

    threads = [threading.Thread(target=writer, args=(value,))
               for value in ("first", "second")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sum(result["ok"] for result in results) == 1
    assert sum("stale" in (result["error"] or "") for result in results) == 1


def test_safe_host_default_and_launcher_guidance():
    import subprocess
    env = os.environ.copy()
    env.pop("MCP_HOST", None)
    result = subprocess.run(
        ["python", "-c", "import server; print(server.HOST)"],
        cwd=pathlib.Path(server.__file__).parent,
        env=env, text=True, capture_output=True, check=True)
    assert result.stdout.strip() == "127.0.0.1"
    for path in ("bin/mcpsh", "install.sh"):
        text = pathlib.Path(path).read_text()
        assert '${MCP_HOST:-127.0.0.1}' in text
        assert "non-loopback MCP_HOST without MCP_AUTH_TOKEN" in text


def test_mcp_visible_descriptions_contain_recovery_guidance():
    descriptions = {
        name: server.mcp._tool_manager._tools[name].description
        for name in ("write_file", "read_file", "read_files", "edit_file",
                     "edit_files", "run_command")}
    assert "cannot create" in descriptions["edit_file"]
    assert "line_numbers=false" in descriptions["edit_file"]
    assert "dry_run" in descriptions["edit_file"]
    assert "never add a newline" in descriptions["edit_file"]
    assert "Each item" in descriptions["edit_files"]
    assert "display-only" in descriptions["read_file"]
    assert "line_numbers" in descriptions["read_files"]
    assert "create or replace" in descriptions["write_file"]
    assert "$TMPDIR" in descriptions["run_command"]
    assert "never rewritten" in descriptions["run_command"]
