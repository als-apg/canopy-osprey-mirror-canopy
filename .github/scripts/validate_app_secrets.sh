#!/usr/bin/env bash
# Validate one APP_SECRETS_<INSTANCE> repo secret before a deploy.
#
# The secret is the ONLY config source for an instance (no env files are
# committed), so a bad secret must fail fast with a clear message instead of
# an opaque jq trace inside software-factory's deploy.sh or a health-check
# timeout after a full deploy attempt. In particular, deploy.sh renders
# (._shared * .<facility>) into the app's .env and silently defaults api_port
# to 8080 when API_PORT is absent - wrong for the 8081 instances.
#
# Single source of truth for the required-keys list; called by deploy.yml
# (every selected instance) and build-image.yml (DEV auto-deploy).
#
# Env in:
#   SECRET_NAME   name of the secret, for error messages only
#   SECRET_VALUE  the secret's content (never echoed, masked immediately)
#   FACILITY      facility key inside the secret JSON, e.g. CONTAINER_LCLS
set -euo pipefail

: "${SECRET_NAME:?SECRET_NAME is required}"
: "${FACILITY:?FACILITY is required}"

if [ -z "${SECRET_VALUE:-}" ]; then
  echo "::error::${SECRET_NAME} is empty. All per-instance config (API_PORT, EPICS_*, URLs) lives in this secret - there are no committed env files. See DEPLOYMENT.md for the required JSON shape."
  exit 1
fi
echo "::add-mask::${SECRET_VALUE}"

if ! printf '%s' "$SECRET_VALUE" | jq -e 'type == "object"' >/dev/null 2>&1; then
  echo "::error::${SECRET_NAME} is not a JSON object. Re-store it as JSON (same shape software-factory/deploy.sh expects); a KEY=value/dotenv blob fails the jq merge."
  exit 1
fi

missing=$(printf '%s' "$SECRET_VALUE" | jq -r --arg fac "$FACILITY" '
  ((._shared // {}) * (.[$fac] // {})) as $m
  | ["API_PORT", "CANOPY_DATABASE_URL", "CANOPY_REDIS_URL"]
  | map(select((($m[.] // "") | tostring | length) == 0))
  | join(", ")')
if [ -n "$missing" ]; then
  echo "::error::${SECRET_NAME} is missing required key(s) under \"${FACILITY}\" (or _shared): ${missing}. See DEPLOYMENT.md for the full secret shape."
  exit 1
fi

echo "${SECRET_NAME} validated."
