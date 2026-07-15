# termux-mcp-shell

Streamable HTTP MCP server that gives an agent shell and file access inside Termux.

## Install

### One-liner

```sh
curl -fsSL https://raw.githubusercontent.com/nelvinzfx/termux-mcp-shell/master/install.sh | sh
```

The installer installs Python, Git, and Termux's native Rust build toolchain,
clones or updates the repository at `~/termux-mcp-shell`, installs Python
dependencies, creates `mcpsh` and `mcpsh-stop`, and adds the repository's `bin`
directory to detected Bash, Zsh, or Fish configuration. Rust is required because
PyPI does not provide Android wheels for `pydantic-core`; the installer prepares
`maturin` and disables build isolation so pip uses Termux's Rust instead of the
unsupported rustup Android target. It is safe to rerun.

Use another destination or repository with:

```sh
MCP_DEST=$HOME/mcp MCP_REPO_URL=https://github.com/example/fork \
  sh -c 'curl -fsSL https://raw.githubusercontent.com/nelvinzfx/termux-mcp-shell/master/install.sh | sh'
```

### Manual

```sh
pkg install python python-pip git rust make pkg-config patchelf
python -m pip install --upgrade "setuptools>=70.1" wheel "maturin>=1.10,<2"
python -m pip install --no-build-isolation -r requirements.txt
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
| `MCP_TRUNC_LIMIT` | `8192` | Initial command-output bytes returned |
| `MCP_MAX_SESSIONS` | `50` | In-memory command-output buffers |
| `MCP_READ_MAX_LINES` | `2000` | Maximum lines per text read |
| `MCP_READ_MAX_BYTES` | `51200` | Approximate byte cap per text read |
| `MCP_AUTH_TOKEN` | unset | Optional shared Bearer/X-API-Key token |

## Tools

### Shell and output

`run_command(command, timeout?, cwd?)` runs `/bin/sh -c` asynchronously. Timeout
or cancellation kills the command's complete process group. Large stdout/stderr
responses include a `session_id` and continuation offsets for
`read_output(session_id, stream, offset, length)`.

### Reading files

`read_file(path, offset=1, limit=null, line_numbers=true)` returns paginated
UTF-8 text plus the exact file SHA-256. Set `line_numbers=false` when copying
source into an exact edit. `read_files(reads)` batches up to 20 such ranges.
`read_file_bytes(path, offset=0, length=4096)` returns Base64 for binary or
minified data.

Filesystem work runs in worker threads, so slow storage does not block unrelated
MCP requests.

Android does not provide `/tmp`. File-tool paths under `/tmp` and a
`run_command` `cwd` under `/tmp` are mapped to Termux's writable `$TMPDIR`.
Responses return the actual mapped path so later shell commands can reuse it.
Literal `/tmp/...` text inside `run_command.command` is deliberately not
rewritten; use the returned path or `$TMPDIR/...` there.

### Writing files

`write_file(path, content)` atomically replaces one UTF-8 file.
`append_file(path, content, expected_sha256=null)` atomically appends and can
reject a stale current file. Both create parent directories and return the
resulting SHA-256.

### Editing files

`edit_file(path, edits, dry_run=false, expected_sha256=null)` edits one file.
`edit_files(files, dry_run=false)` applies the same operation atomically across
multiple files. Inputs are native arrays. Each edit has one explicit shape:

```json
{
  "mode": "replace_match | insert_before | insert_after",
  "match_text": "unique text or anchor",
  "write_text": "literal replacement or insertion"
}
```

Example transaction:

```json
{
  "files": [
    {
      "path": "src/A.kt",
      "expected_sha256": "hash-from-read_file",
      "edits": [
        {
          "mode": "replace_match",
          "match_text": "val enabled = false",
          "write_text": "val enabled = true"
        }
      ]
    },
    {
      "path": "src/B.kt",
      "edits": [
        {
          "mode": "insert_after",
          "match_text": "fun stop() {}",
          "write_text": "\nfun reset() {}"
        }
      ]
    }
  ],
  "dry_run": true
}
```

Every match must resolve uniquely. Matching supports normalized Unicode,
trailing-whitespace tolerance, and indentation-insensitive blocks. Fuzzy matching
only locates the original source span; unmatched text is never normalized or
rewritten. Overlapping edits and multiple operations at the same source position
are rejected before writing. The server validates every file before writing
anything, preserves UTF-8 BOM, line endings, and permission modes, always returns
diffs, and attempts rollback if publishing one file fails. `dry_run` previews
without writes; apply the same payload later with `expected_sha256` values to
reject stale sources.

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
