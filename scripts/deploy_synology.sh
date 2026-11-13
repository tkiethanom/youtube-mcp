#!/usr/bin/env bash
# Executed over encrypted stdin by deploy_over_ssh.sh; never enable shell tracing.
set +x
set -euo pipefail
umask 077
export PATH="$PATH:/usr/local/bin:/usr/syno/bin:/usr/bin:/bin"
export GIT_TERMINAL_PROMPT=0

project=${1:?Missing project path}
expected_sha=${2:?Missing tested commit}
docker_bin=${3:-/usr/local/bin/docker}
[[ "$project" == /* && "$expected_sha" =~ ^[a-f0-9]{40}$ && "$docker_bin" == /* ]] || exit 2
cd -- "$project"
[[ "$(git rev-parse --show-toplevel)" == "$(pwd -P)" ]] || { echo 'Project directory must be the repository root' >&2; exit 2; }
[[ "$(git symbolic-ref --short HEAD)" == main ]] || { echo 'NAS checkout must be on main' >&2; exit 2; }
[[ -z "$(git status --porcelain)" ]] || { echo 'NAS checkout has local changes; refusing to overwrite them' >&2; exit 2; }
[[ -r .env && -d data && -f compose.yaml ]] || { echo 'Existing .env, data and compose.yaml are required' >&2; exit 2; }
command -v sudo >/dev/null
command -v curl >/dev/null
[[ -x "$docker_bin" ]] || { echo 'Set NAS_DOCKER_BIN to the absolute Docker executable path' >&2; exit 2; }

# Refresh sudo authorization before each command, including after a long build.
# Authenticate separately so password bytes never become a container's stdin.
docker_cmd() {
  if [[ -n "${DEPLOY_SUDO_PASSWORD:-}" ]]; then
    printf '%s\n' "$DEPLOY_SUDO_PASSWORD" | sudo -S -p '' -v >/dev/null
  fi
  sudo -n "$docker_bin" "$@" </dev/null
}
project_name=''
compose() {
  local compose_args=(--project-directory "$project" -f "$project/compose.yaml")
  if [[ -n "$project_name" ]]; then compose_args+=(--project-name "$project_name"); fi
  docker_cmd compose "${compose_args[@]}" "$@"
}

docker_cmd compose version >/dev/null
compose config --quiet
container_id=$(compose ps -q shelf)
[[ -n "$container_id" && "$container_id" != *$'\n'* ]] || { echo 'Expected one existing shelf container for safe rollback' >&2; exit 2; }
previous_sha=$(git rev-parse HEAD)
previous_image=$(docker_cmd inspect --format '{{.Image}}' "$container_id")
[[ -n "$previous_image" ]] || { echo 'Cannot identify the existing service image' >&2; exit 2; }
project_name=$(docker_cmd inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$container_id")
[[ "$project_name" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || { echo 'Cannot identify the existing Compose project' >&2; exit 2; }
mkdir -p backups
chmod 700 backups
lock_dir="$project/backups/.deploy-lock"
mkdir "$lock_dir" || { echo 'Deployment lock exists; check for a running/stalled deployment' >&2; exit 2; }
checkout_changed=false
service_stopped=false
deployment_healthy=false
backup_dir=''
rollback_tag="youtube-mcp-rollback:$(date -u +%Y%m%dT%H%M%SZ)-${expected_sha:0:12}-$$"

finish() {
  result=$?
  trap - EXIT INT TERM HUP
  set +e
  if ((result != 0)); then
    echo 'Deployment failed.' >&2
    if ! $deployment_healthy; then
      if $checkout_changed && git diff --quiet && git diff --cached --quiet && [[ "$(git rev-parse HEAD)" == "$expected_sha" ]]; then
        git reset --hard "$previous_sha" >/dev/null || echo 'Could not restore previous Git revision' >&2
      fi
      if $service_stopped; then
        echo 'Attempting to restart the previous image without restoring/erasing database data.' >&2
        docker_cmd compose --project-directory "$project" --project-name "$project_name" \
          -f "$backup_dir/compose.previous.yaml" -f "$backup_dir/rollback-compose.json" \
          up -d --no-build --no-deps --force-recreate shelf || \
          echo 'AUTOMATIC RECOVERY FAILED: inspect the NAS service manually.' >&2
      fi
    fi
    [[ -z "$backup_dir" ]] || echo "Private backup/recovery files: $backup_dir" >&2
  fi
  rmdir "$lock_dir"
  unset DEPLOY_SUDO_PASSWORD
  exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

backup_dir="$project/backups/deploy-$(date -u +%Y%m%dT%H%M%SZ)-${expected_sha:0:12}-$$"
mkdir "$backup_dir"
cp -p compose.yaml "$backup_dir/compose.previous.yaml"
cp -p .env "$backup_dir/.env"
chmod 600 "$backup_dir/.env"
printf '{"services":{"shelf":{"image":"%s"}}}\n' "$rollback_tag" > "$backup_dir/rollback-compose.json"
printf '%s\n' "$previous_sha" > "$backup_dir/previous-commit"
printf '%s\n' "$expected_sha" > "$backup_dir/target-commit"

git fetch origin main </dev/null
[[ "$(git rev-parse origin/main)" == "$expected_sha" ]] || { echo 'main has moved since tests; rerun the workflow for the new commit' >&2; exit 1; }
[[ -z "$(git ls-tree -r --name-only "$expected_sha" -- .env data)" ]] || { echo 'Refusing a revision that tracks private configuration/data' >&2; exit 1; }
# Merge exactly the revision that passed tests; a concurrently newer main is not deployed.
git merge --ff-only "$expected_sha" </dev/null
checkout_changed=true
[[ "$(git rev-parse HEAD)" == "$expected_sha" ]] || { echo 'Checkout does not match the tested commit' >&2; exit 1; }
compose config --quiet
docker_cmd image tag "$previous_image" "$rollback_tag"
# Keep the current container running while dependencies/images build.
compose build shelf
service_stopped=true
compose stop -t 30 shelf
# The service drops all capabilities; the one-off backup needs to read files
# owned by the application UID and write the private host-owned backup folder.
compose run --rm --no-deps -T --user 0:0 --cap-add DAC_OVERRIDE \
  --volume "$project/data:/data:ro" \
  --volume "$backup_dir:/backup:rw" \
  --volume "$project/scripts/backup_deployment.py:/backup_deployment.py:ro" \
  shelf python /backup_deployment.py /data /backup
[[ -f "$backup_dir/BACKUP_COMPLETE" ]] || { echo 'Backup did not complete; refusing to replace the running image' >&2; exit 1; }
compose up -d --no-build --no-deps --force-recreate shelf

for ((attempt=1; attempt<=36; attempt++)); do
  current_id=$(compose ps -q shelf)
  if [[ -n "$current_id" ]] && \
     [[ "$(docker_cmd inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$current_id")" == healthy ]] && \
     curl -fsS --max-time 5 http://127.0.0.1:8787/healthz >/dev/null 2>&1; then
    deployment_healthy=true
    printf '%s\n' "$expected_sha" > "$backup_dir/deployed-commit"
    echo "Deployed and healthy: $expected_sha"
    echo "Private backup: $backup_dir"
    exit 0
  fi
  sleep 5
done
compose ps shelf
echo 'Service did not become healthy; restoring the previous image.' >&2
exit 1
