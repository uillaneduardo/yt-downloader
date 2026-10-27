#!/usr/bin/env bash
set -Eeuo pipefail
: "${IMAGE_REF:?IMAGE_REF required}"
: "${DEPLOY_DIR:?Set HOMELAB_COMPOSE_DIR}"
: "${DEPLOY_SERVICE:?Set HOMELAB_COMPOSE_SERVICE}"
[[ "$DEPLOY_DIR" == /* ]] || { echo "Absolute DEPLOY_DIR required"; exit 1; }
[[ "$DEPLOY_SERVICE" =~ ^[a-zA-Z0-9_-]+$ ]] || exit 1
cd "$DEPLOY_DIR"
test -f compose.yml || test -f compose.yaml || test -f docker-compose.yml || test -f docker-compose.yaml
lock="/tmp/homelab-deploy-${DEPLOY_SERVICE}.lock"
exec 9>"$lock"
flock -n 9 || { echo "Another deploy for this service is running"; exit 1; }
# The compose service must use image: ${HOMELAB_IMAGE} and have a healthcheck.
prev="$(docker compose config --format json | python3 -c 'import json,sys,os; s=json.load(sys.stdin)["services"][os.environ["DEPLOY_SERVICE"]]; print(s.get("image",""))')"
[[ -n "$prev" ]] || { echo "Compose service must declare an image"; exit 1; }
docker pull "$IMAGE_REF"
rollback() {
  echo "Deploy failed; restoring previous image: $prev"
  HOMELAB_IMAGE="$prev" docker compose up -d --no-deps --force-recreate "$DEPLOY_SERVICE" || true
}
trap rollback ERR
HOMELAB_IMAGE="$IMAGE_REF" docker compose config --quiet
HOMELAB_IMAGE="$IMAGE_REF" docker compose up -d --no-deps --force-recreate "$DEPLOY_SERVICE"
cid="$(HOMELAB_IMAGE="$IMAGE_REF" docker compose ps -q "$DEPLOY_SERVICE")"
[[ -n "$cid" ]] || exit 1
for i in $(seq 1 36); do
  state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "$cid")"
  [[ "$state" == healthy ]] && { trap - ERR; echo "Healthy: $IMAGE_REF"; exit 0; }
  [[ "$state" == unhealthy || "$state" == missing ]] && { echo "Health status: $state"; exit 1; }
  sleep 5
done
echo "Healthcheck timeout"; exit 1
