#!/usr/bin/env bash
set -Eeuo pipefail
: "${IMAGE_REF:?IMAGE_REF required}"
: "${DEPLOY_DIR:?Set HOMELAB_COMPOSE_DIR}"
: "${DEPLOY_SERVICE:?Set HOMELAB_COMPOSE_SERVICE}"
[[ "$DEPLOY_DIR" == /* && -d "$DEPLOY_DIR" ]] || { echo "Invalid absolute deploy directory"; exit 1; }
[[ "$DEPLOY_SERVICE" =~ ^[a-zA-Z0-9_-]+$ ]] || exit 1
cd "$DEPLOY_DIR"
exec 9>"/tmp/homelab-deploy-${DEPLOY_SERVICE}.lock"
flock -n 9 || { echo "Another deployment is running"; exit 1; }
# Verify that only the specified service references the injected image.
declared="$(HOMELAB_IMAGE=ci-contract-probe docker compose config --format json | python3 -c 'import json,sys,os; d=json.load(sys.stdin); print(d["services"][os.environ["DEPLOY_SERVICE"]].get("image",""))')"
[[ "$declared" == ci-contract-probe ]] || { echo "Compose image must be ${HOMELAB_IMAGE}"; exit 1; }
old_id="$(HOMELAB_IMAGE="$IMAGE_REF" docker compose ps -q "$DEPLOY_SERVICE" || true)"
previous=""
if [[ -n "$old_id" ]]; then
  previous="$(docker inspect --format '{{.Config.Image}}' "$old_id")"
fi
docker pull "$IMAGE_REF"
rollback() {
  echo "Deploy failed: attempting rollback to $previous"
  if [[ -n "$previous" ]]; then
    HOMELAB_IMAGE="$previous" docker compose up -d --no-deps --force-recreate "$DEPLOY_SERVICE" || true
  else
    echo "No previous container; manual intervention required"
  fi
}
trap rollback ERR
HOMELAB_IMAGE="$IMAGE_REF" docker compose config --quiet
HOMELAB_IMAGE="$IMAGE_REF" docker compose up -d --no-deps --force-recreate "$DEPLOY_SERVICE"
cid="$(HOMELAB_IMAGE="$IMAGE_REF" docker compose ps -q "$DEPLOY_SERVICE")"
[[ -n "$cid" ]] || { echo "Container not started"; exit 1; }
for i in $(seq 1 36); do
  state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$cid")"
  if [[ "$state" == healthy ]]; then
    trap - ERR
    echo "Deploy healthy: $IMAGE_REF"
    exit 0
  fi
  if [[ "$state" == unhealthy || "$state" == missing ]]; then
    echo "Container health check failed: $state"
    exit 1
  fi
  sleep 5
done
echo "Container health check timed out"
exit 1
