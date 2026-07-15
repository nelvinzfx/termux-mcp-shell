# termux-mcp-shell

Streamable HTTP MCP server that gives an agent shell and file access inside Termux.

## Install

### One-liner

```sh
curl -fsSL https://raw.githubusercontent.com/nelvinzfx/termux-mcp-shell/master/install.sh | sh
```

The installer installs Python and Git, clones or updates the repository at
`~/termux-mcp-shell`, installs Python dependencies, creates `mcpsh` and
`mcpsh-stop`, and adds the repository's `bin` directory to detected Bash, Zsh,
or Fish configuration. It is safe to rerun.

Use another destination or repository with:

```sh
MCP_DEST=$HOME/mcp MCP_REPO_URL=https://github.com/example/fork \
  sh -c 'curl -fsSL https://raw.githubusercontent.com/nelvinzfx/termux-mcp-shell/master/install.sh | sh'
```

### Manual

```sh
pkg install python git
pip install -r requirements.txt
python server.py
```

Only the `mcp` SDK is a direct Python dependency. The server otherwise uses the
Python standard library.

## Run and stop

Foreground:

```sh
python server.py
```

Background, surviving terminal-tab closure:

```sh
mcpsh
mcpsh-stop
```

`mcpsh` writes the PID to `~/.mcpsh.pid`, logs to `~/.mcpsh.log`, and prints the
local and detected LAN endpoints. The default MCP endpoint is:

```text
http://127.0.0.1:8088/mcp
```

The server binds `0.0.0.0:8088` by default.

## Configuration

| Environment variable | Default | Purpose |
|---|---:|---|
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_PORT` | `8088` | HTTP port |
| `MCP_TRUNC_LIMIT` | `8192` | Initial stdout/stderr bytes returned by `run_command` |
| `MCP_MAX_SESSIONS` | `50` | In-memory command-output buffers |
| `MCP_READ_MAX_LINES` | `2000` | Maximum lines returned by one `read_file` call |
| `MCP_READ_MAX_BYTES` | `51200` | Approximate text-byte cap per `read_file` page |
| `MCP_AUTH_TOKEN` | unset | Optional shared Bearer/X-API-Key token |
| `EDIT_PLAN_TTL_SECONDS` | `600` | Edit-plan lifetime in this server process |
| `EDIT_PLAN_CAPACITY` | `50` | Maximum live edit plans before FIFO eviction |

## Tools

### Shell and output

#### `run_command(command, timeout?, cwd?)`

Runs `/bin/sh -c`. The response includes exit status, timeout state, initial
stdout/stderr, total byte counts, truncation flags, and continuation offsets.
A compound shell command may have produced side effects before returning a
nonzero status.

#### `read_output(session_id, stream="stdout", offset=0, length=4096)`

Reads a byte range from buffered stdout or stderr. Buffers are process-local and
FIFO-evicted after `MCP_MAX_SESSIONS` entries.

### Reading files

#### `read_file(path, offset=1, limit=null, line_numbers=true)`

Returns paginated UTF-8 text, metadata, and `sha256`, calculated from the exact
current file bytes. `offset` is 1-indexed. Set `line_numbers=false` for raw text
that preserves line endings and can be copied into exact edits. Large files are
capped by `MCP_READ_MAX_LINES` and `MCP_READ_MAX_BYTES`; use `next_offset` to
continue.

The returned hash can protect a later edit from overwriting concurrent changes:

```json
{
  "path": "src/App.kt",
  "offset": 1,
  "limit": 200
}
```

```json
{
  "path": "src/App.kt",
  "edits": [{"old_text": "old", "new_text": "new"}],
  "expected_sha256": "sha256-returned-by-read_file"
}
```

#### `read_files(reads)`

Batch-reads up to 20 text-file ranges in input order. Each item accepts `path`,
`offset`, `limit`, and `line_numbers` with the same semantics as `read_file`.
Missing files return item-level errors without discarding successful reads.

#### `read_file_bytes(path, offset=0, length=4096)`

Returns an arbitrary byte range as Base64. Use it for binary, minified, or
otherwise unsuitable text files.

Filesystem tools run in worker threads so slow storage and hashing do not block
unrelated MCP requests. Mutating tools remain serialized to preserve transaction
ordering.

### Writing files

#### `write_file(path, content, sha256=null)`

Atomically writes UTF-8 content, creating parent directories. Here `sha256`
verifies the bytes of the supplied **new payload** before writing.

#### `append_file(path, content, sha256=null)`

Atomically appends UTF-8 content, creating the file and parent directories when
needed. Its optional `sha256` also verifies the supplied **payload**.

The payload-integrity hash accepted by `write_file` and `append_file` differs
from `expected_sha256`: the latter verifies the current source file before an
edit and prevents stale overwrites.

## Single-file edits

### `edit_file(path, edits, dry_run=false, partial=false, expected_sha256=null)`

`edits` accepts a native array or a JSON-encoded array. Legacy replacements stay
supported:

```json
{
  "path": "src/App.kt",
  "expected_sha256": "current-source-hash",
  "edits": [
    {"old_text": "val enabled = false", "new_text": "val enabled = true"}
  ]
}
```

Explicit modes are also available:

```json
{
  "path": "src/App.kt",
  "edits": [
    {
      "mode": "replace_match",
      "match_text": "val mode = OLD",
      "write_text": "val mode = NEW"
    },
    {
      "mode": "insert_before",
      "anchor": "fun start() {",
      "content": "// inserted literally\n"
    },
    {
      "mode": "insert_after",
      "anchor": "fun stop() {}",
      "content": "\nfun reset() {}"
    }
  ]
}
```

Every match or anchor must resolve uniquely. Matching tries exact, normalized
whitespace/Unicode, and indentation-insensitive forms. Inserted `content` is
literal: the server never adds a newline, so include every desired `\n`.
Overlapping edits are rejected.

With `partial=false`, any failed edit aborts the file. `partial=true` applies
independent successful edits and reports failures. `dry_run=true` returns the
unified diff without writing. UTF-8 BOM and CRLF/LF style are preserved.

Per-edit results are structured and bounded. A missing match can include:

```json
{
  "index": 0,
  "mode": "replace_match",
  "matched": false,
  "status": "failed",
  "reason": "match not found",
  "match_count": 0,
  "closest_match_line": 327,
  "similarity": 0.94,
  "nearby_text": ">> 327: val conversationRepo = ..."
}
```

Ambiguous matches include a bounded candidate list instead of guessing.

## Multi-file atomic edits

### `edit_files(files=null, dry_run=false, return_diff=true, validate_all=true, create_plan=false, apply_plan=null)`

`files` accepts a native list or JSON-encoded list. Each item contains `path`,
`edits`, and optional `expected_sha256`.

Direct transaction:

```json
{
  "files": [
    {
      "path": "src/SubAgentRun.kt",
      "expected_sha256": "hash-a",
      "edits": [{"old_text": "old A", "new_text": "new A"}]
    },
    {
      "path": "src/ChatList.kt",
      "expected_sha256": "hash-b",
      "edits": [{"mode": "insert_after", "anchor": "anchor B", "content": "\nnew B"}]
    }
  ],
  "dry_run": false,
  "return_diff": true,
  "validate_all": true
}
```

Preview without writes:

```json
{
  "files": [
    {"path": "src/A.kt", "edits": [{"old_text": "a", "new_text": "b"}]},
    {"path": "src/B.kt", "edits": [{"old_text": "c", "new_text": "d"}]}
  ],
  "dry_run": true,
  "return_diff": true,
  "validate_all": true
}
```

The response contains per-file source/result hashes, diagnostics, and optional
unified diffs. `validate_all=true` collects independent validation failures while
still writing nothing.

Paths are canonicalized, so duplicate paths and symlink aliases in one
transaction are rejected. Validation and publication share a process-wide lock,
preventing two in-process transactions from both validating stale state and then
overwriting each other. All files, hashes, encodings, and edits are validated
before publication.

Publication uses same-directory temporary files followed by `os.replace`, while
preserving UTF-8 BOM, line endings, and permission modes. If a replacement fails,
the server attempts to restore every already-replaced file and reports rollback
status. This provides atomic validation and best-effort multi-file rollback. It
cannot guarantee filesystem-wide atomicity across a process crash, power loss,
or filesystems that do not honor normal atomic-rename semantics.

## Preview plans

Create a plan from a successful preview:

```json
{
  "files": [
    {"path": "src/A.kt", "edits": [{"old_text": "before", "new_text": "after"}]}
  ],
  "dry_run": true,
  "return_diff": true,
  "validate_all": true,
  "create_plan": true
}
```

Apply it without resending the edit payload:

```json
{
  "apply_plan": "opaque-plan-id"
}
```

`apply_plan` must be used without `files`. Application revalidates every source
hash under the transaction lock. A stale plan is rejected without writing.
Plans are opaque, process-local, and disappear on restart. They expire after
`EDIT_PLAN_TTL_SECONDS` and are FIFO-evicted above `EDIT_PLAN_CAPACITY`. A plan is
one-shot only after successful application; a failed stale or publication attempt
remains available until expiry. Missing, expired, evicted, stale, and reused
plans return distinct errors.

Plan IDs are capabilities: anyone able to call this MCP server and obtain an ID
may attempt to apply it during its lifetime. Use authentication and trusted
transport boundaries.

## Authentication

By default the server has no authentication. Anyone who can reach it can execute
commands and read or modify files. Restrict it to a trusted network or set:

```sh
MCP_AUTH_TOKEN="$(openssl rand -hex 32)" mcpsh
```

Clients may send either:

```text
Authorization: Bearer <token>
```

or:

```text
X-API-Key: <token>
```

Authentication uses one shared token. There is no TLS, per-client identity, or
rate limiting. For exposure outside loopback or a trusted private network, place
the server behind TLS and stronger access controls.
