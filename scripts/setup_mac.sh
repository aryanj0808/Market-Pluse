#!/usr/bin/env bash
# Market Pulse - one-shot setup for macOS (Apple Silicon or Intel).
#
#   From a fresh Mac:        bash setup_mac.sh https://github.com/<you>/market-pulse.git
#   From inside the clone:   bash scripts/setup_mac.sh
#
# Installs what is missing (Homebrew, git, gh, uv, Python 3.12, Docker Desktop), clones the repo,
# creates .env (asks for optional keys, generates the database password), builds a synthetic dataset
# whose dates end today, starts every service in Docker and opens the dashboard.
# Safe to re-run: existing .env values, data and Docker volumes are kept.
#
# Options (environment variables):
#   MP_DIR=~/market-pulse   where to clone (fresh-Mac mode)
#   SKIP_SECRETS=1          do not ask for API keys (fill .env later)
#   SKIP_DATA=1             do not regenerate the synthetic dataset
set -euo pipefail

REPO_URL="${1:-}"
MP_DIR="${MP_DIR:-$HOME/market-pulse}"
DASHBOARD="http://127.0.0.1:8000"

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

[ "$(uname -s)" = "Darwin" ] || die "this script is for macOS"

# ---------------------------------------------------------------- tools
say "Checking tools"
if ! have brew; then
  for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$p" ] && eval "$("$p" shellenv)"; done
fi
if ! have brew; then
  say "Installing Homebrew (asks for your Mac password)"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$p" ] && eval "$("$p" shellenv)"; done
  have brew || die "Homebrew install failed"
fi
ok "Homebrew $(brew --version | head -1 | awk '{print $2}')"

for f in git gh uv; do
  if have "$f"; then ok "$f"; else say "Installing $f"; brew install "$f"; fi
done

if ! have docker; then
  say "Installing Docker Desktop"
  brew install --cask docker
fi
if ! docker info >/dev/null 2>&1; then
  say "Starting Docker (first launch: accept the licence in the Docker window)"
  open -ga Docker 2>/dev/null || open -a Docker
  for _ in $(seq 1 90); do docker info >/dev/null 2>&1 && break; sleep 2; done
  docker info >/dev/null 2>&1 || die "Docker did not start - open Docker Desktop, finish its setup, re-run this script"
fi
docker compose version >/dev/null 2>&1 || die "'docker compose' (v2) not found - update Docker Desktop"
ok "Docker $(docker version --format '{{.Server.Version}}')"

# ---------------------------------------------------------------- repository
say "Locating the repository"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$HERE/../infra/docker-compose.yml" ]; then
  ROOT="$(cd "$HERE/.." && pwd)"
elif [ -f "$MP_DIR/infra/docker-compose.yml" ]; then
  ROOT="$MP_DIR"
else
  [ -n "$REPO_URL" ] || die "not inside the repo - run: bash setup_mac.sh <repo-url>"
  if ! gh auth status >/dev/null 2>&1; then
    say "Log in to GitHub (needed for a private repo)"
    gh auth login --hostname github.com --git-protocol https --web
  fi
  gh auth setup-git >/dev/null 2>&1 || true
  git clone "$REPO_URL" "$MP_DIR"
  ROOT="$MP_DIR"
fi
cd "$ROOT"
ok "repo: $ROOT"

# ---------------------------------------------------------------- .env
get_env() { # value of KEY in .env ("" if missing)
  awk -F= -v k="$1" '$1==k {sub(/^[^=]*=/, ""); v=$0} END {print v}' .env
}
set_env() { # replace or append KEY=value, keep every other line
  local tmp; tmp="$(mktemp)"
  awk -v k="$1" -v v="$2" 'BEGIN{done=0} $0 ~ "^"k"=" {print k"="v; done=1; next} {print} END{if(!done) print k"="v}' \
    .env >"$tmp" && mv "$tmp" .env
}
ask_secret() { # KEY "prompt" - only when empty in .env; hidden input; Enter skips
  [ -n "$(get_env "$1")" ] && { ok "$1 already set"; return; }
  [ "${SKIP_SECRETS:-0}" = "1" ] && return
  local v; printf '    %s (Enter to skip): ' "$2"; read -rs v </dev/tty; echo
  [ -n "$v" ] && set_env "$1" "$v" && ok "$1 saved"
}

say "Configuring .env"
if [ ! -f .env ]; then cp .env.example .env; ok "created .env from .env.example"; fi
chmod 600 .env

pw="$(get_env POSTGRES_PASSWORD)"
if [ -z "$pw" ] || [ "$pw" = "change-me" ]; then
  if docker volume ls --format '{{.Name}}' | grep -q '^market-pulse_'; then
    warn "Docker volumes from an earlier run exist; keeping the current POSTGRES_PASSWORD"
  else
    pw="$(openssl rand -hex 16)"
    set_env POSTGRES_PASSWORD "$pw"
    ok "generated a database password"
  fi
fi
pu="$(get_env POSTGRES_USER)"; pdb="$(get_env POSTGRES_DB)"
set_env DATABASE_URL "postgresql://${pu:-pulse}:${pw}@127.0.0.1:5432/${pdb:-market_pulse}"

echo "    Optional keys - all can be added to .env later:"
ask_secret ANTHROPIC_API_KEY  "Anthropic API key (news classification + Ask Claude)"
ask_secret KITE_API_KEY       "Zerodha Kite api_key"
ask_secret KITE_API_SECRET    "Zerodha Kite api_secret"
ask_secret TELEGRAM_BOT_TOKEN "Telegram bot token (alerts)"
ask_secret TELEGRAM_CHAT_ID   "Telegram chat id"

# ---------------------------------------------------------------- python
say "Python environment"
uv python install 3.12
uv sync
ok "uv sync done"

# ---------------------------------------------------------------- data
mkdir -p data/kite data/upstox
if [ "${SKIP_DATA:-0}" != "1" ]; then
  say "Synthetic replay dataset (25 trading days ending today; prices are fake, tagged source=synthetic)"
  dates="$(uv run python -c '
from datetime import date, timedelta
d, days = date.today(), []
while len(days) < 25:
    if d.weekday() < 5:
        days.append(d)
    d -= timedelta(days=1)
days.reverse()
print(days[0].isoformat(), days[-5].isoformat() + "T09:15")
')"
  START="${dates% *}"; REPLAY_START="${dates#* }"
  uv run mp synth --start "$START" --days 25
  uv run mp synth-news
  set_env MP_REPLAY_START "$REPLAY_START"   # replay the last 5 days (one market minute per second)
  ok "replay starts at $REPLAY_START IST"
fi

# ---------------------------------------------------------------- docker
say "Building and starting services (first build takes a few minutes)"
docker compose -f infra/docker-compose.yml --env-file .env up -d --build --wait
docker compose -f infra/docker-compose.yml --env-file .env ps --format 'table {{.Service}}\t{{.Status}}'

for _ in $(seq 1 30); do curl -fs "$DASHBOARD/api/health" >/dev/null && break; sleep 2; done
if curl -fs "$DASHBOARD/api/health" | grep -q '"all_ok": *true'; then ok "all services healthy"; else
  warn "health check not all green yet - see: docker compose -f infra/docker-compose.yml logs --tail 50"; fi
open "$DASHBOARD" || true

cat <<EOF

Done. Dashboard: $DASHBOARD   (repo: $ROOT)

Next steps
  Live Zerodha data (each morning; token expires ~6 AM IST):
    cd "$ROOT"
    uv run mp kite-login        # open the URL, log in, paste the redirect URL back
    uv run mp kite-check        # verify against the real API
    # set MP_PROVIDER=kite in .env, then:
    docker compose -f infra/docker-compose.yml --env-file .env up -d

  Stop everything:   docker compose -f infra/docker-compose.yml --env-file .env down
  Logs:              docker compose -f infra/docker-compose.yml --env-file .env logs -f ingest-market
EOF
