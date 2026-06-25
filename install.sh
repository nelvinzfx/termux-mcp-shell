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

# 4. fish helper functions (only if fish is installed)
if command -v fish >/dev/null 2>&1; then
    FISH_CFG="$HOME/.config/fish/config.fish"
    mkdir -p "$(dirname "$FISH_CFG")"
    touch "$FISH_CFG"
    if ! grep -q "function mcpsh" "$FISH_CFG" 2>/dev/null; then
        log "Adding fish functions mcpsh / mcpsh-stop"
        cat >> "$FISH_CFG" <<EOF

# termux-mcp-shell
function mcpsh --description 'start termux-mcp-shell server in background'
    stdbuf -oL -eL python $DEST/server.py >~/.mcpsh.log 2>&1 &
    disown
    echo "mcpsh started, log: ~/.mcpsh.log"
end

function mcpsh-stop --description 'stop termux-mcp-shell server'
    pkill -f $DEST/server.py; and echo stopped; or echo "not running"
end
EOF
    else
        log "fish functions already present, skipping"
    fi
    HINT="open a new fish tab (or: source $FISH_CFG) then run: mcpsh"
else
    HINT="start it with: python $DEST/server.py"
fi

log "Done. Installed at $DEST"
echo
echo "Start the server: $HINT"
echo "MCP endpoint:     http://127.0.0.1:8088/mcp"
