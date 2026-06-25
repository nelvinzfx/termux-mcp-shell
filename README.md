# termux-mcp-shell

MCP server (Streamable HTTP) that gives an agent full shell access on Termux,
plus pi-grade file read/write/edit tools.

## Install

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
| `MCP_TRUNC_LIMIT`    | `4096`    | max stdout/stderr bytes per reply   |
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
  at 2000 lines / 50KB and returns `next_offset` to continue.
- **`write_file(path, content)`**: create or overwrite a file; parent dirs are
  created automatically.
- **`edit_file(path, edits)`**: exact-text replacement, modeled after pi.
  `edits` is a list of `{old_text, new_text}`. Each `old_text` must match
  exactly and be unique, is matched against the original content, must not
  overlap, and all edits apply atomically (all-or-nothing). If an exact match
  fails, a fuzzy match is tried (trailing whitespace, smart quotes, unicode
  dashes/spaces). CRLF line endings and BOM are preserved. Returns a unified diff.

## Security note

No auth. Anyone on the same network can execute commands. Use only on a trusted
network (your own device / private hotspot).
