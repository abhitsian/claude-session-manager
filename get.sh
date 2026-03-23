#!/bin/bash
#
# Claude Desk — one-click installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/abhitsian/claude-desk/main/get.sh | bash
#

set -e

INSTALL_DIR="$HOME/.claude-desk"
PYTHON="$(which python3 2>/dev/null)"
PLIST_DIR="$HOME/Library/LaunchAgents"
REPO="https://github.com/abhitsian/claude-desk.git"
PORT=8080

echo ""
echo "  Claude Desk — the UI for Claude Code"
echo "  ======================================"
echo ""

# Check prerequisites
if [ -z "$PYTHON" ]; then
    echo "  Error: python3 not found. Install Python 3.9+ first."
    exit 1
fi

if [ ! -d "$HOME/.claude" ]; then
    echo "  Error: ~/.claude not found. Install Claude Code first."
    echo "  https://docs.anthropic.com/en/docs/claude-code"
    exit 1
fi

echo "  Installing to: $INSTALL_DIR"
echo ""

# Clone or update
if [ -d "$INSTALL_DIR" ]; then
    echo "  Updating existing installation..."
    cd "$INSTALL_DIR"
    git pull --quiet origin main
else
    echo "  Downloading..."
    git clone --quiet "$REPO" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# Install Python deps
echo "  Installing dependencies..."
pip3 install -q fastapi uvicorn jinja2 python-multipart pydantic pydantic-settings aiofiles 2>/dev/null

# Stop existing services
launchctl unload "$PLIST_DIR/com.claude.desk.plist" 2>/dev/null || true
launchctl unload "$PLIST_DIR/com.claude.desk-archiver.plist" 2>/dev/null || true
# Also stop old session-manager if present
launchctl unload "$PLIST_DIR/com.claude.session-manager.plist" 2>/dev/null || true
launchctl unload "$PLIST_DIR/com.claude.session-archiver.plist" 2>/dev/null || true

# Kill any process on the port
lsof -ti :$PORT 2>/dev/null | xargs kill -9 2>/dev/null || true
sleep 1

# Create dashboard LaunchAgent
echo "  Setting up dashboard server..."
mkdir -p "$PLIST_DIR"

cat > "$PLIST_DIR/com.claude.desk.plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude.desk</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>claude_sessions.main:app</string>
        <string>--port</string>
        <string>$PORT</string>
        <string>--host</string>
        <string>127.0.0.1</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$INSTALL_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/claude-desk.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/claude-desk.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
EOF

launchctl load "$PLIST_DIR/com.claude.desk.plist"

# Create archiver LaunchAgent
echo "  Setting up daily archiver..."

cat > "$PLIST_DIR/com.claude.desk-archiver.plist" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude.desk-archiver</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$INSTALL_DIR/archive_cron.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$INSTALL_DIR</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>3</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/claude-desk-archiver.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/claude-desk-archiver.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
    </dict>
</dict>
</plist>
EOF

launchctl load "$PLIST_DIR/com.claude.desk-archiver.plist"

# Run initial archive
echo "  Archiving existing sessions..."
cd "$INSTALL_DIR"
$PYTHON archive_cron.py --all 2>/dev/null

# Wait for server to start
sleep 3

# Open in browser
echo ""
echo "  ======================================"
echo "  Claude Desk is running!"
echo ""
echo "  Dashboard:  http://localhost:$PORT"
echo "  Archiver:   Daily at 3 AM"
echo "  Install:    $INSTALL_DIR"
echo ""
echo "  To update:  cd $INSTALL_DIR && git pull"
echo "  To uninstall:"
echo "    launchctl unload ~/Library/LaunchAgents/com.claude.desk.plist"
echo "    launchctl unload ~/Library/LaunchAgents/com.claude.desk-archiver.plist"
echo "    rm -rf $INSTALL_DIR"
echo "  ======================================"
echo ""

open "http://localhost:$PORT"
