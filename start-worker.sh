#!/usr/bin/env bash
set -euo pipefail

# Env expected:
#   AWS_REGION        (e.g., us-east-1)
#   S3_BUCKET         (e.g., headturner)
#   HANDLER_S3_KEY    (e.g., handlers/rp_handler.py)
#
# Baked fallback:
#   /workspace/_baked_rp_handler.py
#
# ComfyUI venv Python:
#   /workspace/ComfyUI/.venv/bin/python

AWS_REGION="${AWS_REGION:-us-east-1}"
S3_BUCKET="${S3_BUCKET:?Missing S3_BUCKET}"
S3_KEY="${HANDLER_S3_KEY:?Missing HANDLER_S3_KEY}"

LIVE="/workspace/rp_handler.py"
BAKED="/workspace/_baked_rp_handler.py"
PY="/workspace/ComfyUI/.venv/bin/python"

log(){ printf '[boot] %s\n' "$*" >&2; }
fetch(){ aws s3 cp "s3://${S3_BUCKET}/${S3_KEY}" "$1" --region "$AWS_REGION" --no-progress; }

mkdir -p /workspace

log "fetching handler from s3://${S3_BUCKET}/${S3_KEY}"
if fetch "$LIVE"; then
  chmod +x "$LIVE" || true
  log "handler fetched"
else
  log "S3 fetch failed — using baked handler"
  cp -f "$BAKED" "$LIVE"
fi

# Show version banner (first line) for audit; safe if file lacks newline
head -n 1 "$LIVE" || true

# Exec the handler (handler should call runpod.serverless.start(...) in __main__)
exec "$PY" "$LIVE"
