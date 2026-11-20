#!/bin/bash
# Daily refresh for the HK transaction dashboard.
#
# Runs on Chris's Mac rather than GitHub Actions because 28Hse and Midland
# permanently block GitHub's shared runner IP ranges (403 / 0 records).
# The Mac's own IP is not blocked, so all three providers resolve.
#
# Steps: fetch -> update -> source guard -> commit -> push -> (Pages redeploy
# is triggered by the push via the workflow's deploy job).

set -uo pipefail

PROJECT_DIR="$HOME/Projects/hk-transaction-dashboard"
LOG_DIR="$HOME/Library/Logs/hk-transaction-dashboard"
LOG_FILE="$LOG_DIR/update.log"
PAT_FILE="$HOME/.config/hk-transaction-dashboard/pat"

mkdir -p "$LOG_DIR"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"
}

cd "$PROJECT_DIR" || { log "FATAL: cannot cd to $PROJECT_DIR"; exit 1; }

# The PAT is stored outside the repo and outside the conversation.
if [ ! -f "$PAT_FILE" ]; then
  log "FATAL: PAT file missing at $PAT_FILE"
  exit 1
fi
PAT=$(cat "$PAT_FILE")
export GH_TOKEN="$PAT"

log "--- daily refresh starting"

# Pull remote changes first so a push never races the bot's commit.
git fetch origin --quiet || log "WARN: git fetch failed"

# Stash any uncommitted work (code edits) so the rebase can proceed. Never
# hard-reset: that would silently discard local edits.
STASHED=0
if [ -n "$(git status --porcelain)" ]; then
  git stash push --include-untracked -m "daily-refresh-autostash" >> "$LOG_FILE" 2>&1 && STASHED=1
  log "stashed local changes before rebase"
fi

if ! git rebase origin/main >> "$LOG_FILE" 2>&1; then
  log "WARN: rebase conflict; aborting and keeping local commits"
  git rebase --abort >> "$LOG_FILE" 2>&1 || true
  # Resolve data-file conflicts toward the complete dataset rather than
  # resetting the tree, so local code changes survive.
  if git diff --quiet HEAD -- data/transactions.json 2>/dev/null; then
    git reset --soft origin/main
    log "reset --soft onto origin/main (local commits preserved)"
  fi
fi

if [ "$STASHED" -eq 1 ]; then
  git stash pop >> "$LOG_FILE" 2>&1 || log "WARN: could not restore stashed changes; they remain in 'git stash list'"
fi

# Run the updater with retries; providers occasionally time out.
UPDATED=0
for attempt in 1 2 3; do
  log "updater attempt $attempt"
  if python3 scripts/update_data.py >> "$LOG_FILE" 2>&1; then
    if python3 scripts/check_sources.py >> "$LOG_FILE" 2>&1; then
      UPDATED=1
      log "all sources returned records on attempt $attempt"
      break
    fi
    log "WARN: source check failed on attempt $attempt"
  else
    log "WARN: updater exited non-zero on attempt $attempt"
  fi
  sleep 30
done

if [ "$UPDATED" -ne 1 ]; then
  log "ERROR: could not get all sources; keeping previous dataset, not pushing"
  git checkout -- data/transactions.json data/source-state.json data/update-report.json 2>/dev/null || true
  exit 1
fi

# Commit the refreshed dataset.
git config user.name  "hk-transaction-dashboard"
git config user.email "hk-transaction-dashboard@localhost"
git add data/transactions.json data/update-report.json data/source-state.json
if git diff --staged --quiet; then
  log "no dataset changes; nothing to push"
  exit 0
fi
git commit -m "data: refresh estate transactions" >> "$LOG_FILE" 2>&1 || {
  log "ERROR: commit failed"; exit 1;
}

# Push with a temporary auth header so the token never lands in git config.
AUTH_HEADER=$(printf 'x-access-token:%s' "$PAT" | base64)
if git -c http.extraheader="AUTHORIZATION: basic $AUTH_HEADER" push origin main >> "$LOG_FILE" 2>&1; then
  log "pushed dataset to origin/main"
else
  log "ERROR: push failed"
  exit 1
fi

log "--- daily refresh done"
