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
log "Installing python + git (pkg)"
pkg install -y python git >/dev/null 2>&1 || {
    echo "pkg install failed. Run 'pkg update' first?" >&2; exit 1; }

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
log "Installing Python dependencies"
pip install -r "$DEST/requirements.txt"

# 4. create mcpsh / mcpsh-stop scripts
log "Creating mcpsh / mcpsh-stop commands"
mkdir -p "$DEST/bin"

cat > "$DEST/bin/mcpsh" <<'MCPSCRIPT'
#!/data/data/com.termux/files/usr/bin/sh
set -eu
PIDFILE="$HOME/.mcpsh.pid"
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
    echo "  MCP endpoints:"
    echo "    local: http://127.0.0.1:$PORT/mcp"
    # Detect LAN IP
    LAN_IP=""
    if command -v ifconfig >/dev/null 2>&1; then
        LAN_IP=$(ifconfig 2>/dev/null | grep 'inet ' | grep -v '127.0.0.1' | awk '{print $2}' | head -1)
    elif command -v ip >/dev/null 2>&1; then
        LAN_IP=$(ip addr 2>/dev/null | grep 'inet ' | grep -v '127.0.0.1' | awk '{print $2}' | cut -d/ -f1 | head -1)
    fi
    if [ -n "$LAN_IP" ]; then
        echo "    LAN:   http://$LAN_IP:$PORT/mcp"
    else
        echo "    LAN:   (could not auto-detect, run: ifconfig)"
    fi
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
