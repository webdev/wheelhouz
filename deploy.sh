#!/usr/bin/env bash
set -euo pipefail

# --- Configuration ---
SSH_KEY="$HOME/.ssh/id_ed25519_hetzner"
SSH_OPTS="-i $SSH_KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
SERVER="root@65.108.214.129"
REMOTE_DIR="/opt/wheelhouz"
SERVICE_NAME="wheelhouz"

ssh_cmd() { ssh $SSH_OPTS "$SERVER" "$@"; }
scp_cmd() { scp $SSH_OPTS "$@"; }

echo "=== Wheelhouz Deploy ==="

# --- Step 1: Sync code ---
echo "[1/5] Syncing code to server..."
rsync -avz --delete \
  -e "ssh $SSH_OPTS" \
  --exclude '.env' \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '.mypy_cache' \
  --exclude '.venv' \
  --exclude 'venv' \
  --exclude '.ralph' \
  --exclude '.ralphrc' \
  --exclude '*.svg' \
  --exclude 'config/.etrade_tokens.json' \
  --exclude 'config/.tv_cache' \
  --exclude 'config/.scanner_cache.json' \
  --exclude 'config/.shopping_list_cache.csv' \
  --exclude 'config/.shopping_list_fetched' \
  --exclude 'config/.ticker_map.json' \
  /Users/gblazer/workspace/wheelhouz/ "$SERVER:$REMOTE_DIR/"

# --- Step 2: Sync secrets ---
echo "[2/5] Syncing secrets..."
scp_cmd /Users/gblazer/workspace/wheelhouz/.env "$SERVER:$REMOTE_DIR/.env"
scp_cmd /Users/gblazer/workspace/wheelhouz/config/.etrade_tokens.json "$SERVER:$REMOTE_DIR/config/.etrade_tokens.json"

# --- Step 3: Install uv + dependencies on server ---
echo "[3/5] Installing dependencies on server..."
ssh_cmd "bash -s" <<'REMOTE_SETUP'
set -euo pipefail

cd /opt/wheelhouz

# Install uv if missing
if ! command -v uv &>/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
export PATH="$HOME/.local/bin:$PATH"

# Create venv and install deps
uv sync

# Create cache dirs
mkdir -p config/.tv_cache
REMOTE_SETUP

# --- Step 4: Set up systemd service ---
echo "[4/5] Setting up systemd service..."
ssh_cmd "bash -s" <<'REMOTE_SYSTEMD'
set -euo pipefail

cat > /etc/systemd/system/wheelhouz.service <<'EOF'
[Unit]
Description=Wheelhouz Options Trading Copilot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/wheelhouz
EnvironmentFile=/opt/wheelhouz/.env
ExecStart=/root/.local/bin/uv run python -m src.main --mode briefing
Restart=no
StandardOutput=journal
StandardError=journal
SyslogIdentifier=wheelhouz

[Install]
WantedBy=multi-user.target
EOF

# Briefing timer — 8:00 AM ET (12:00 UTC during EDT)
cat > /etc/systemd/system/wheelhouz-briefing.service <<'EOF'
[Unit]
Description=Wheelhouz Morning Briefing
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/opt/wheelhouz
EnvironmentFile=/opt/wheelhouz/.env
ExecStart=/root/.local/bin/uv run python -m src.main --mode briefing
StandardOutput=journal
StandardError=journal
SyslogIdentifier=wheelhouz-briefing
EOF

cat > /etc/systemd/system/wheelhouz-briefing.timer <<'EOF'
[Unit]
Description=Run Wheelhouz morning briefing at 8am ET

[Timer]
OnCalendar=*-*-* 12:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable wheelhouz-briefing.timer
systemctl start wheelhouz-briefing.timer

echo "Timer status:"
systemctl list-timers wheelhouz-briefing.timer --no-pager
REMOTE_SYSTEMD

# --- Step 5: Verify ---
echo "[5/5] Verifying deployment..."
ssh_cmd "bash -s" <<'REMOTE_VERIFY'
export PATH="$HOME/.local/bin:$PATH"
cd /opt/wheelhouz
echo "--- Python version ---"
uv run python --version
echo "--- Quick import test ---"
uv run python -c "from src.config.loader import load_watchlist; print('Import OK')"
echo "--- .env loaded ---"
grep -c '=' /opt/wheelhouz/.env || echo "warning: .env looks empty"
echo "--- Timer ---"
systemctl is-enabled wheelhouz-briefing.timer
REMOTE_VERIFY

echo ""
echo "=== Deploy complete ==="
echo ""
echo "Commands:"
echo "  Run briefing now:   ssh $SSH_OPTS $SERVER 'cd $REMOTE_DIR && uv run python -m src.main --mode briefing'"
echo "  View logs:          ssh $SSH_OPTS $SERVER 'journalctl -u wheelhouz-briefing -n 50'"
echo "  Redeploy:           ./deploy.sh"
echo ""
echo "IMPORTANT: Edit $REMOTE_DIR/.env on the server to add:"
echo "  ANTHROPIC_API_KEY=sk-ant-..."
echo "  (Remove AWS_BEARER_TOKEN_BEDROCK if set)"
