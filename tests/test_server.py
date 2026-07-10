import base64
import hashlib
import json
import os
import threading

import server


# ---------------------------------------------------------------------------
# Existing baseline tests (preserved)
# ---------------------------------------------------------------------------

def test_edit_noop_is_success(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("same\n")
    result = server.edit_file(str(path), json.dumps([{"old_text": "same", "new_text": "same"}]))
    assert result["ok"] is True
    assert result["changed"] is False
    assert result["replacements"] == 0
    assert result["results"][0]["status"] == "matched_no_change"
    assert path.read_text() == "same\n"


def test_atomic_failure_has_per_edit_diagnostics_and_does_not_write(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("one\ntwo\n")
    result = server.edit_file(str(path), [
        {"old_text": "one", "new_text": "ONE"},
        {"old_text": "missing", "new_text": "MISSING"},
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


def test_edit_accepts_legacy_json_string_and_native_array(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("a")
    assert server.edit_file(str(path), '[{"old_text":"a","new_text":"b"}]')["ok"]
    assert server.edit_file(str(path), [{"old_text": "b", "new_text": "c"}])["ok"]
    assert path.read_text() == "c"


def test_append_is_atomic_creates_parent_and_reports_metadata(tmp_path):
    path = tmp_path / "nested" / "file.txt"
    result = server.append_file(str(path), "hello")
    assert result["ok"] is True
    assert result["bytes_written"] == 5
    assert result["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert path.read_text() == "hello"
    failed = server.append_file(str(path), "!", "0" * 64)
    assert failed["ok"] is False
    assert path.read_text() == "hello"


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
    digest = hashlib.sha256(payload.encode()).hexdigest()
    result = server.write_file(str(path), payload, digest)
    assert result["ok"] is True
    assert result["bytes_written"] == len(payload.encode())
    assert path.read_text() == payload


def test_run_command_truncation_has_next_offsets(monkeypatch):
    monkeypatch.setattr(server, "TRUNC_LIMIT", 3)
    result = server.run_command("printf 123456; printf abcdef >&2")
    assert result["stdout_truncated"] is True
    assert result["stderr_truncated"] is True
    assert result["stdout_next_offset"] == 3
    assert result["stderr_next_offset"] == 3
    assert result["session_id"]
    assert server.read_output(result["session_id"], "stdout", 3, 3)["data"] == "456"


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


# ---------------------------------------------------------------------------
# edit_file expected_sha256 stale protection
# ---------------------------------------------------------------------------

def test_edit_file_expected_sha256_success(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("alpha\n")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    result = server.edit_file(str(path), [{"old_text": "alpha", "new_text": "beta"}],
                              expected_sha256=sha)
    assert result["ok"] is True
    assert path.read_text() == "beta\n"


def test_edit_file_expected_sha256_stale(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("alpha\n")
    result = server.edit_file(str(path), [{"old_text": "alpha", "new_text": "beta"}],
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
        {"mode": "insert_before", "anchor": "b", "content": "INSERTED"}
    ])
    assert result["ok"]
    assert path.read_text() == "a\nINSERTEDb\nc\n"


def test_insert_after_mode(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("a\nb\nc\n")
    result = server.edit_file(str(path), [
        {"mode": "insert_after", "anchor": "b", "content": "INSERTED"}
    ])
    assert result["ok"]
    assert path.read_text() == "a\nbINSERTED\nc\n"


def test_insert_before_with_newline_content(tmp_path):
    """Literal newline in content is preserved; no extra newline is added."""
    path = tmp_path / "f.txt"
    path.write_text("a\nb\nc\n")
    result = server.edit_file(str(path), [
        {"mode": "insert_before", "anchor": "b", "content": "X\nY\n"}
    ])
    assert result["ok"]
    assert path.read_text() == "a\nX\nY\nb\nc\n"


def test_insert_after_with_newline_content(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("a\nb\nc\n")
    result = server.edit_file(str(path), [
        {"mode": "insert_after", "anchor": "b", "content": "\nX\nY"}
    ])
    assert result["ok"]
    assert path.read_text() == "a\nb\nX\nY\nc\n"


def test_legacy_mode_still_works_alongside_explicit(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("one\ntwo\nthree\n")
    result = server.edit_file(str(path), [
        {"old_text": "one", "new_text": "ONE"},
        {"mode": "insert_after", "anchor": "two", "content": "2.5"},
        {"mode": "replace_match", "match_text": "three", "write_text": "THREE"},
    ])
    assert result["ok"]
    assert path.read_text() == "ONE\ntwo2.5\nTHREE\n"


# ---------------------------------------------------------------------------
# Ambiguous / missing anchors and bounded diagnostics
# ---------------------------------------------------------------------------

def test_ambiguous_anchor_rejected(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("dup\ndup\nother\n")
    result = server.edit_file(str(path), [
        {"mode": "insert_before", "anchor": "dup", "content": "X"}
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
        {"mode": "insert_before", "anchor": "hello wrld", "content": "X"}
    ])
    assert not result["ok"]
    r = result["results"][0]
    assert not r["matched"]
    assert "closest_match_line" in r
    assert "similarity" in r
    assert "nearby_text" in r
    assert r["closest_match_line"] == 1


def test_missing_old_text_has_closest_match(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("def foo():\n    return 42\n")
    result = server.edit_file(str(path), [
        {"old_text": "def foo:", "new_text": "X"}
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
        {"old_text": "nonexistent text here", "new_text": "X"}
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
        {"path": str(p1), "edits": [{"old_text": "alpha", "new_text": "ALPHA"}]},
        {"path": str(p2), "edits": [{"old_text": "beta", "new_text": "BETA"}]},
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
        assert f["diff"]  # return_diff defaults to True


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
        {"path": str(p1), "edits": [{"old_text": "alpha", "new_text": "ALPHA"}]},
        {"path": str(p2), "edits": [{"old_text": "nonexistent", "new_text": "X"}]},
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
         "edits": [{"old_text": "alpha", "new_text": "BETA"}],
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
        {"path": str(p1), "edits": [{"old_text": "alpha", "new_text": "X"}],
         "expected_sha256": sha1},
        {"path": str(p2), "edits": [{"old_text": "beta", "new_text": "Y"}],
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
        {"path": str(p), "edits": [{"old_text": "alpha", "new_text": "X"}]},
        {"path": str(p), "edits": [{"old_text": "alpha", "new_text": "Y"}]},
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
        {"path": str(target), "edits": [{"old_text": "data", "new_text": "X"}]},
        {"path": str(link), "edits": [{"old_text": "data", "new_text": "Y"}]},
    ])
    assert not result["ok"]
    assert "duplicate" in result["error"].lower()


# ---------------------------------------------------------------------------
# Dry-run no-write, per-file diff, validate_all
# ---------------------------------------------------------------------------

def test_dry_run_no_write(tmp_path):
    p1 = tmp_path / "a.txt"
    p2 = tmp_path / "b.txt"
    p1.write_text("alpha\n")
    p2.write_text("beta\n")
    orig1 = p1.read_bytes()
    orig2 = p2.read_bytes()
    result = server.edit_files([
        {"path": str(p1), "edits": [{"old_text": "alpha", "new_text": "ALPHA"}]},
        {"path": str(p2), "edits": [{"old_text": "beta", "new_text": "BETA"}]},
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
        {"path": str(p1), "edits": [{"old_text": "alpha", "new_text": "ALPHA"}]},
        {"path": str(p2), "edits": [{"old_text": "beta", "new_text": "BETA"}]},
    ], dry_run=True, return_diff=True)
    assert result["ok"]
    diffs = [f["diff"] for f in result["files"]]
    assert all(d for d in diffs)
    assert "ALPHA" in diffs[0]
    assert "BETA" in diffs[1]


def test_dry_run_source_and_result_sha256(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("alpha\n")
    result = server.edit_files([
        {"path": str(p), "edits": [{"old_text": "alpha", "new_text": "beta"}]},
    ], dry_run=True)
    assert result["ok"]
    f = result["files"][0]
    assert f["sha256"] == hashlib.sha256(b"alpha\n").hexdigest()
    assert f["result_sha256"] == hashlib.sha256(b"beta\n").hexdigest()


def test_validate_all_false_applies_successful_files(tmp_path):
    p1 = tmp_path / "a.txt"
    p2 = tmp_path / "b.txt"
    p1.write_text("alpha\n")
    p2.write_text("beta\n")
    result = server.edit_files([
        {"path": str(p1), "edits": [{"old_text": "alpha", "new_text": "ALPHA"}]},
        {"path": str(p2), "edits": [{"old_text": "nonexistent", "new_text": "X"}]},
    ], validate_all=False)
    assert result["ok"]  # no top-level error
    assert result["applied"]
    assert p1.read_text() == "ALPHA\n"
    assert p2.read_text() == "beta\n"  # unchanged
    files = {f["path"]: f for f in result["files"]}
    assert files[str(p1)]["ok"]
    assert not files[str(p2)]["ok"]


# ---------------------------------------------------------------------------
# Plan create / apply by id only
# ---------------------------------------------------------------------------

def test_plan_create_and_apply(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("alpha\n")
    # Create plan
    dry = server.edit_files([
        {"path": str(p), "edits": [{"old_text": "alpha", "new_text": "beta"}]},
    ], dry_run=True, create_plan=True)
    assert dry["ok"]
    assert dry["plan_id"]
    plan_id = dry["plan_id"]
    assert p.read_text() == "alpha\n"  # not written

    # Apply plan
    applied = server.edit_files(apply_plan=plan_id)
    assert applied["ok"]
    assert applied["applied"]
    assert p.read_text() == "beta\n"


def test_plan_apply_rejects_simultaneous_files(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("alpha\n")
    dry = server.edit_files([
        {"path": str(p), "edits": [{"old_text": "alpha", "new_text": "beta"}]},
    ], dry_run=True, create_plan=True)
    plan_id = dry["plan_id"]
    result = server.edit_files(
        files=[{"path": str(p), "edits": [{"old_text": "alpha", "new_text": "X"}]}],
        apply_plan=plan_id,
    )
    assert not result["ok"]
    assert "cannot specify both" in result["error"].lower()


def test_plan_reused_after_successful_apply(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("alpha\n")
    dry = server.edit_files([
        {"path": str(p), "edits": [{"old_text": "alpha", "new_text": "beta"}]},
    ], dry_run=True, create_plan=True)
    plan_id = dry["plan_id"]
    first = server.edit_files(apply_plan=plan_id)
    assert first["ok"]
    second = server.edit_files(apply_plan=plan_id)
    assert not second["ok"]
    assert "reused" in second["error"].lower()


def test_plan_stale_during_apply(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("alpha\n")
    dry = server.edit_files([
        {"path": str(p), "edits": [{"old_text": "alpha", "new_text": "beta"}]},
    ], dry_run=True, create_plan=True)
    plan_id = dry["plan_id"]
    # Modify file externally
    p.write_text("gamma\n")
    result = server.edit_files(apply_plan=plan_id)
    assert not result["ok"]
    assert "stale" in result["error"].lower()
    # Plan should still be available (not consumed)
    # Restore original content and retry
    p.write_text("alpha\n")
    retry = server.edit_files(apply_plan=plan_id)
    assert retry["ok"]
    assert p.read_text() == "beta\n"


def test_plan_failed_apply_remains_available(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("alpha\n")
    dry = server.edit_files([
        {"path": str(p), "edits": [{"old_text": "alpha", "new_text": "beta"}]},
    ], dry_run=True, create_plan=True)
    plan_id = dry["plan_id"]
    # Simulate stale (file modified)
    p.write_text("gamma\n")
    stale_result = server.edit_files(apply_plan=plan_id)
    assert not stale_result["ok"]
    # Plan should still be available
    p.write_text("alpha\n")
    retry = server.edit_files(apply_plan=plan_id)
    assert retry["ok"]
    assert retry["applied"]


def test_plan_missing(tmp_path):
    result = server.edit_files(apply_plan="nonexistent_plan_id_12345")
    assert not result["ok"]
    assert "missing" in result["error"].lower()


def test_plan_expired(monkeypatch, tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("alpha\n")
    dry = server.edit_files([
        {"path": str(p), "edits": [{"old_text": "alpha", "new_text": "beta"}]},
    ], dry_run=True, create_plan=True)
    plan_id = dry["plan_id"]
    # Force expiry by setting TTL to 0
    monkeypatch.setattr(server, "_PLAN_TTL", 0)
    import time as _time
    _time.sleep(0.01)
    result = server.edit_files(apply_plan=plan_id)
    assert not result["ok"]
    assert "expired" in result["error"].lower()


def test_plan_capacity_eviction(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_PLAN_CAPACITY", 2)
    # Clear existing plans
    with server._PLAN_LOCK:
        server._PLANS.clear()
        server._REMOVED.clear()

    p = tmp_path / "a.txt"
    p.write_text("alpha\n")
    plan_ids = []
    for i in range(3):
        dry = server.edit_files([
            {"path": str(p), "edits": [{"old_text": "alpha", "new_text": f"v{i}"}]},
        ], dry_run=True, create_plan=True)
        # Reset file for next plan
        p.write_text("alpha\n")
        plan_ids.append(dry["plan_id"])

    # First plan should be evicted
    result = server.edit_files(apply_plan=plan_ids[0])
    assert not result["ok"]
    assert "evicted" in result["error"].lower()

    # Last plan should still be available (not evicted or missing)
    p.write_text("alpha\n")
    result2 = server.edit_files(apply_plan=plan_ids[2])
    # It may succeed or be stale, but must not be evicted or missing
    if not result2["ok"]:
        assert "evicted" not in result2["error"].lower()
        assert "missing" not in result2["error"].lower()


# ---------------------------------------------------------------------------
# BOM / CRLF / mode preservation
# ---------------------------------------------------------------------------

def test_bom_crlf_mode_preservation_in_edit_files(tmp_path):
    p = tmp_path / "f.txt"
    content = "\ufeffline1\r\nline2\r\n"
    p.write_bytes(content.encode("utf-8"))
    original_mode = p.stat().st_mode & 0o777
    result = server.edit_files([
        {"path": str(p), "edits": [{"old_text": "line1", "new_text": "LINE1"}]},
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
        {"old_text": "hello", "new_text": "HELLO"},
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
        {"path": str(p1), "edits": [{"old_text": "one", "new_text": "ONE"}]},
        {"path": str(p2), "edits": [{"old_text": "two", "new_text": "TWO"}]},
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
             "edits": [{"old_text": "b", "new_text": f"v{idx}"}],
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
# JSON string / native array legacy behavior in edit_files
# ---------------------------------------------------------------------------

def test_edit_files_accepts_json_string(tmp_path):
    p1 = tmp_path / "a.txt"
    p2 = tmp_path / "b.txt"
    p1.write_text("alpha\n")
    p2.write_text("beta\n")
    payload = json.dumps([
        {"path": str(p1), "edits": [{"old_text": "alpha", "new_text": "ALPHA"}]},
        {"path": str(p2), "edits": [{"old_text": "beta", "new_text": "BETA"}]},
    ])
    result = server.edit_files(payload)
    assert result["ok"]
    assert p1.read_text() == "ALPHA\n"
    assert p2.read_text() == "BETA\n"


def test_edit_files_accepts_native_array(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("alpha\n")
    result = server.edit_files([
        {"path": str(p), "edits": [{"old_text": "alpha", "new_text": "ALPHA"}]},
    ])
    assert result["ok"]
    assert p.read_text() == "ALPHA\n"


def test_edit_files_edits_as_json_string(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("alpha\n")
    result = server.edit_files([
        {"path": str(p),
         "edits": json.dumps([{"old_text": "alpha", "new_text": "ALPHA"}])},
    ])
    assert result["ok"]
    assert p.read_text() == "ALPHA\n"


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
        {"path": str(p), "edits": [{"old_text": "x", "new_text": "y"}]},
    ])
    assert not result["ok"]
    assert "utf-8" in result["error"].lower() or "unicode" in result["error"].lower()
