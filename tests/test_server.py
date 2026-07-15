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


def test_public_tool_signatures_are_minimal():
    import inspect
    assert list(inspect.signature(server.write_file).parameters) == ["path", "content"]
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
