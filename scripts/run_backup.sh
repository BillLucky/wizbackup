#!/bin/sh
# Safe to run from any cwd. Never changes the machine's global proxy settings.
set -eu
umask 077
PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
cd "$PROJECT"
WIZBACKUP_DATA_DIR=${WIZBACKUP_DATA_DIR:-"$HOME/WizBackupData"}
exec "$PROJECT/.venv/bin/python" -u -m wizbackup.cli backup-all \
  --auth "$WIZBACKUP_DATA_DIR/private/auth.json" \
  --password-file "$WIZBACKUP_DATA_DIR/private/decryption-password.txt" \
  --output "$WIZBACKUP_DATA_DIR/backups/account" --workers 4 "$@"
