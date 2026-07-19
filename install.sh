#!/data/data/com.termux/files/usr/bin/sh
# termux-mcp-shell installer. Idempotent: safe to re-run.
# Usage:
#   sh install.sh                 # install from this directory
#   curl -fsSL <raw-url>/install.sh | sh   # remote one-liner (clones the repo)
set -eu

REPO_URL="${MCP_REPO_URL:-https://github.com/nelvinzfx/termux-mcp-shell}"
DEST="${MCP_DEST:-$HOME/termux-mcp-shell}"

log() { printf '\033[1;36m==>\033[0m %s\n' "$1"; }

# 1. system deps
# PyPI does not publish Android wheels for pydantic-core. Install Rust and its
# matching host standard library explicitly: pkg can otherwise leave rust-std at
# an older version and fail much later during a native build.
case "$(dpkg --print-architecture 2>/dev/null || true)" in
    aarch64) RUST_STD_PACKAGE="rust-std-aarch64-linux-android" ;;
    arm) RUST_STD_PACKAGE="rust-std-armv7-linux-androideabi" ;;
    i686) RUST_STD_PACKAGE="rust-std-i686-linux-android" ;;
    x86_64) RUST_STD_PACKAGE="rust-std-x86-64-linux-android" ;;
    *)
        echo "Unsupported Termux architecture; cannot select the Rust standard library package." >&2
        exit 1
        ;;
esac

log "Installing Python, Git, and native build tools (pkg)"
if ! pkg install -y python python-pip git rust "$RUST_STD_PACKAGE" make pkg-config patchelf python-cryptography; then
    echo "pkg install failed. Run 'pkg update' first?" >&2
    exit 1
fi

rust_version="$(dpkg-query -W -f='${Version}' rust 2>/dev/null || true)"
rust_std_version="$(dpkg-query -W -f='${Version}' "$RUST_STD_PACKAGE" 2>/dev/null || true)"
if [ -z "$rust_version" ] || [ "$rust_version" != "$rust_std_version" ]; then
    echo "Rust package mismatch: rust=${rust_version:-missing}, $RUST_STD_PACKAGE=${rust_std_version:-missing}." >&2
    echo "Run 'pkg update && pkg install rust $RUST_STD_PACKAGE', then rerun this installer." >&2
    exit 1
fi

# cryptography's no-isolation build imports cffi before pip installs the rest of
# the transaction. Termux's native package provides both ahead of that build.
if ! python - <<'PY'
import cffi
import cryptography
PY
then
    echo "python-cryptography did not provide importable cryptography and cffi modules." >&2
    echo "Run 'pkg update && pkg reinstall python-cryptography', then rerun this installer." >&2
    exit 1
fi

# 2. get the source: use current dir if server.py is here, else clone
if [ -f "./server.py" ] && [ -f "./requirements.txt" ]; then
    SRC="$(pwd)"
    if [ "$SRC" != "$DEST" ]; then
        log "Copying source to $DEST"
        mkdir -p "$DEST"
        cp server.py requirements.txt "$DEST/"
        [ -f README.md ] && cp README.md "$DEST/" || true
        [ -f install.sh ] && cp install.sh "$DEST/" || true
    fi
else
    log "Cloning $REPO_URL -> $DEST"
    if [ -d "$DEST/.git" ]; then
        git -C "$DEST" pull --ff-only
    else
        git clone --depth 1 "$REPO_URL" "$DEST"
    fi
fi

# 3. python deps
# Pydantic uses maturin to build pydantic-core. Installing the backend first and
# disabling PEP 517 build isolation prevents pip from trying rustup, which does
# not support Termux's aarch64-unknown-linux-android target.
# Use the active Python build platform's API level. The device OS API may be
# newer, producing wheels that this same Python rejects as incompatible.
PYTHON_ANDROID_API_LEVEL="$(python - <<'PY'
import re
import sysconfig

platform = sysconfig.get_platform()
match = re.fullmatch(r"android-(\d+)-(.+)", platform)
level = sysconfig.get_config_var("ANDROID_API_LEVEL")
if level is None and match:
    level = match.group(1)
try:
    level = int(level)
except (TypeError, ValueError):
    raise SystemExit(f"cannot derive Android API level from Python platform {platform!r}")
if not match or int(match.group(1)) != level:
    raise SystemExit(
        f"inconsistent Python Android platform: platform={platform!r}, API={level!r}")

try:
    from packaging.tags import sys_tags
except ImportError:
    from pip._vendor.packaging.tags import sys_tags
wheel_platform = f"android_{level}_{match.group(2)}"
if not any(tag.platform == wheel_platform for tag in sys_tags()):
    raise SystemExit(
        f"active Python does not accept derived wheel platform {wheel_platform!r}")
print(level)
PY
)" || {
    echo "Failed to derive a compatible Android API level from the active Python." >&2
    exit 1
}
case "$PYTHON_ANDROID_API_LEVEL" in
    ''|*[!0-9]*)
        echo "Active Python returned an invalid Android API level: $PYTHON_ANDROID_API_LEVEL" >&2
        exit 1
        ;;
esac
if [ -n "${ANDROID_API_LEVEL:-}" ] && [ "$ANDROID_API_LEVEL" != "$PYTHON_ANDROID_API_LEVEL" ]; then
    log "Ignoring inherited ANDROID_API_LEVEL=$ANDROID_API_LEVEL; active Python requires $PYTHON_ANDROID_API_LEVEL"
fi
ANDROID_API_LEVEL="$PYTHON_ANDROID_API_LEVEL"
export ANDROID_API_LEVEL
log "Using ANDROID_API_LEVEL=$ANDROID_API_LEVEL from the active Python platform"
log "Preparing Python build backend"
python -m pip install --upgrade "setuptools>=70.1" wheel "maturin>=1.10,<2"

log "Installing Python dependencies"
python -m pip install --no-build-isolation -r "$DEST/requirements.txt"

# 4. create mcpsh / mcpsh-stop scripts
log "Creating mcpsh / mcpsh-stop commands"
mkdir -p "$DEST/bin"

cat > "$DEST/bin/mcpsh" <<'MCPSCRIPT'
#!/data/data/com.termux/files/usr/bin/sh
set -eu
PIDFILE="$HOME/.mcpsh.pid"
HOST="${MCP_HOST:-127.0.0.1}"
PORT="${MCP_PORT:-8088}"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Already running?
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "mcpsh already running (PID $(cat "$PIDFILE"))"
    exit 1
fi

# Start in background, immune to SIGHUP (survives tab close)
nohup python "$SCRIPT_DIR/server.py" > "$HOME/.mcpsh.log" 2>&1 &
echo $! > "$PIDFILE"

sleep 1

if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    PID=$(cat "$PIDFILE")
    echo "mcpsh started (PID $PID)"
    echo "  log: ~/.mcpsh.log"
    echo ""
    case "$HOST" in
        127.0.0.1|localhost)
            echo "  MCP endpoint: http://$HOST:$PORT/mcp"
            echo "  LAN access disabled (safe default)."
            echo "  Set MCP_HOST=0.0.0.0 to enable LAN access."
            ;;
        ::1)
            echo "  MCP endpoint: http://[::1]:$PORT/mcp"
            echo "  LAN access disabled (safe default)."
            echo "  Set MCP_HOST=0.0.0.0 to enable LAN access."
            ;;
        *)
            echo "  MCP bind: $HOST:$PORT"
            if [ -z "${MCP_AUTH_TOKEN:-}" ]; then
                echo "  WARNING: non-loopback MCP_HOST without MCP_AUTH_TOKEN."
                echo "  Set MCP_AUTH_TOKEN before exposing this shell server."
            else
                echo "  token authentication enabled"
            fi
            LAN_IP=""
            if command -v ifconfig >/dev/null 2>&1; then
                LAN_IP=$(ifconfig 2>/dev/null | grep 'inet ' | grep -v '127.0.0.1' | awk '{print $2}' | head -1)
            elif command -v ip >/dev/null 2>&1; then
                LAN_IP=$(ip addr 2>/dev/null | grep 'inet ' | grep -v '127.0.0.1' | awk '{print $2}' | cut -d/ -f1 | head -1)
            fi
            if [ -n "$LAN_IP" ]; then
                echo "  LAN endpoint: http://$LAN_IP:$PORT/mcp"
            fi
            ;;
    esac
else
    echo "mcpsh failed to start, check ~/.mcpsh.log"
    rm -f "$PIDFILE"
    exit 1
fi

MCPSCRIPT
chmod +x "$DEST/bin/mcpsh"

cat > "$DEST/bin/mcpsh-stop" <<'STOPSCRIPT'
#!/data/data/com.termux/files/usr/bin/sh
PIDFILE="$HOME/.mcpsh.pid"
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        sleep 0.5
        kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null
        echo "mcpsh stopped (PID $PID)"
    else
        echo "mcpsh not running (stale pidfile)"
    fi
    rm -f "$PIDFILE"
else
    pkill -f "server.py" 2>/dev/null && echo "mcpsh stopped" || echo "mcpsh not running"
fi
STOPSCRIPT
chmod +x "$DEST/bin/mcpsh-stop"

# 5. Wire PATH into detected shells (bash, zsh, fish)
SHELLS_FOUND=""

setup_rc_sh() {
    rc="$1"
    mkdir -p "$(dirname "$rc")"
    touch "$rc"
    # Remove old entries (marker to end of file)
    awk '/^# termux-mcp-shell/{exit} {print}' "$rc" > "$rc.tmp" 2>/dev/null && mv "$rc.tmp" "$rc" || true
    printf '\n# termux-mcp-shell\nexport PATH="%s/bin:$PATH"\n' "$DEST" >> "$rc"
}

setup_rc_fish() {
    rc="$1"
    mkdir -p "$(dirname "$rc")"
    touch "$rc"
    awk '/^# termux-mcp-shell/{exit} {print}' "$rc" > "$rc.tmp" 2>/dev/null && mv "$rc.tmp" "$rc" || true
    printf '\n# termux-mcp-shell\nset -gx PATH "%s/bin" $PATH\n' "$DEST" >> "$rc"
}

for sh_name in bash zsh; do
    rc="$HOME/.${sh_name}rc"
    if command -v "$sh_name" >/dev/null 2>&1 || [ -f "$rc" ]; then
        setup_rc_sh "$rc"
        log "Configured $rc"
        SHELLS_FOUND="$SHELLS_FOUND $sh_name"
    fi
done

fish_rc="$HOME/.config/fish/config.fish"
if command -v fish >/dev/null 2>&1 || [ -f "$fish_rc" ]; then
    setup_rc_fish "$fish_rc"
    log "Configured $fish_rc"
    SHELLS_FOUND="$SHELLS_FOUND fish"
fi

if [ -z "$SHELLS_FOUND" ]; then
    rc="$HOME/.profile"
    setup_rc_sh "$rc"
    log "Configured $rc (fallback)"
    SHELLS_FOUND=" .profile"
fi

log "Done. Installed at $DEST"
echo
echo "Shells configured:$SHELLS_FOUND"
echo "Open a new terminal tab, then:"
echo "  mcpsh        Start server in background"
echo "  mcpsh-stop   Stop server"
echo "  MCP endpoint: http://127.0.0.1:8088/mcp"
