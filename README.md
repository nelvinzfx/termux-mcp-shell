# termux-mcp-shell

MCP server (Streamable HTTP) that gives an agent full shell access on Termux,
plus pi-grade file read/write/edit tools.

## Install

### One-liner

```sh
curl -fsSL https://raw.githubusercontent.com/nelvinzfx/termux-mcp-shell/master/install.sh | sh
```

Installs python + git via `pkg`, clones the repo to `~/termux-mcp-shell`,
installs Python deps, and (if fish is present) adds `mcpsh` / `mcpsh-stop`
helper functions. Idempotent — safe to re-run.

To use a custom destination or repo URL:

```sh
MCP_DEST=$HOME/mcp sh -c 'curl -fsSL https://raw.githubusercontent.com/nelvinzfx/termux-mcp-shell/master/install.sh | sh'
```

### Manual

```sh
pkg install python
pip install -r requirements.txt
```

Dependencies (see `requirements.txt`): only the `mcp` SDK is required. It pulls
in `uvicorn` + `starlette` (the ASGI runner for the Streamable HTTP transport),
plus `httpx`, `anyio`, `pydantic`, etc. as transitive deps. Everything else the
server uses is from the Python standard library.

## Run

```sh
python server.py
```

Binds `0.0.0.0:8088` by default. MCP endpoint: `/mcp`.

- Local: `http://127.0.0.1:8088/mcp`
- LAN/hotspot: `http://<device-ip>:8088/mcp`

Find the device IP with `ifconfig` or `ip addr`.

## Configuration (env)

| Env                  | Default   | Description                         |
|----------------------|-----------|-------------------------------------|
| `MCP_HOST`           | `0.0.0.0` | bind address                        |
| `MCP_PORT`           | `8088`    | port                                |
| `MCP_TRUNC_LIMIT`    | `8192`    | max stdout/stderr bytes per reply   |
| `MCP_MAX_SESSIONS`   | `50`      | output buffers kept in memory (FIFO)|
| `MCP_READ_MAX_LINES` | `2000`    | max lines per `read_file`           |
| `MCP_READ_MAX_BYTES` | `51200`   | max bytes per `read_file` (50KB)    |

## Tools

- **`run_command(command, timeout?, cwd?)`**: run via `/bin/sh -c`. Output
  larger than `MCP_TRUNC_LIMIT` bytes is truncated; the full output is kept in
  a buffer, get the `session_id` from the result to read the rest.
- **`read_output(session_id, stream?, offset?, length?)`**: read a continued
  chunk from a command's output buffer (`stream`: `stdout`/`stderr`).
- **`read_file(path, offset?, limit?)`**: read a text file with 1-indexed
  line numbers. `offset` = start line, `limit` = number of lines. Auto-truncates
  at 2000 lines / 50KB, appends a clear `--- TRUNCATED ... ---` footer, and
  returns `next_offset` to continue.
- **`write_file(path, content)`**: create or overwrite a file; parent dirs are
  created automatically.
- **`edit_file(path, edits, dry_run?, partial?)`**: text replacement, modeled
  after pi. `edits` is a list of `{old_text, new_text}`. Matching is tried in
  three tiers per edit: exact, then whitespace/smart-quote/unicode tolerant,
  then indent-insensitive (leading whitespace ignored and `new_text` is
  re-indented to fit, handy for indented Python/YAML blocks). Each `old_text`
  must resolve to a unique location and edits must not overlap.
  - `partial=false` (default): atomic, any failed edit aborts and writes nothing.
  - `partial=true`: apply matching edits, skip the rest, report per-edit status
    in `results`.
  - `dry_run=true`: return the diff without writing.

  CRLF line endings and BOM are preserved. Returns a unified diff plus a
  per-edit `results` list.

## Security note

No auth. Anyone on the same network can execute commands. Use only on a trusted
network (your own device / private hotspot).
