#!/usr/bin/env bash
# GitHub runner entry point. Never enable shell tracing in this script.
set +x
set -euo pipefail
umask 077

: "${NAS_SSH_HOST:?Set the NAS_SSH_HOST Actions variable}"
: "${NAS_SSH_USER:?Set NAS_SSH_USER}"
: "${NAS_SSH_PORT:?Set NAS_SSH_PORT}"
: "${NAS_PROJECT_DIR:?Set NAS_PROJECT_DIR}"
: "${NAS_DOCKER_BIN:?Set NAS_DOCKER_BIN}"
: "${NAS_SSH_KNOWN_HOSTS:?Set the verified NAS_SSH_KNOWN_HOSTS Actions secret}"
: "${EXPECTED_SHA:?Missing tested commit SHA}"

[[ "$NAS_SSH_HOST" =~ ^[A-Za-z0-9][A-Za-z0-9.:-]*$ ]] || { echo 'Invalid SSH hostname' >&2; exit 2; }
[[ "$NAS_SSH_USER" =~ ^[A-Za-z_][A-Za-z0-9_.-]*$ ]] || { echo 'Invalid SSH username' >&2; exit 2; }
[[ "$NAS_SSH_PORT" =~ ^[0-9]{1,5}$ ]] && ((10#$NAS_SSH_PORT >= 1 && 10#$NAS_SSH_PORT <= 65535)) || { echo 'Invalid SSH port' >&2; exit 2; }
[[ "$EXPECTED_SHA" =~ ^[a-f0-9]{40}$ ]] || { echo 'Invalid tested commit SHA' >&2; exit 2; }
[[ "$NAS_PROJECT_DIR" == /* && "$NAS_PROJECT_DIR" != *$'\n'* && "$NAS_DOCKER_BIN" == /* ]] || { echo 'Expected absolute NAS paths' >&2; exit 2; }
[[ "${NAS_SUDO_PASSWORD:-}" != *$'\n'* ]] || { echo 'Multiline sudo passwords are not supported' >&2; exit 2; }

ssh_dir=$(mktemp -d "${RUNNER_TEMP:-/tmp}/shelf-ssh.XXXXXX")
trap 'rm -rf -- "$ssh_dir"' EXIT
printf '%s\n' "$NAS_SSH_KNOWN_HOSTS" > "$ssh_dir/known_hosts"
options=(-F /dev/null -T -p "$NAS_SSH_PORT" -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=$ssh_dir/known_hosts" -o GlobalKnownHostsFile=/dev/null
  -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=3
  -o ForwardAgent=no -o ClearAllForwardings=yes -o NumberOfPasswordPrompts=1)

if [[ -n "${NAS_SSH_PRIVATE_KEY:-}" ]]; then
  printf '%s\n' "$NAS_SSH_PRIVATE_KEY" | tr -d '\r' > "$ssh_dir/key"
  chmod 600 "$ssh_dir/key"
  ssh-keygen -y -P '' -f "$ssh_dir/key" >/dev/null
  options+=(-i "$ssh_dir/key" -o IdentitiesOnly=yes -o BatchMode=yes -o PreferredAuthentications=publickey)
else
  : "${NAS_SSH_PASSWORD:?Set NAS_SSH_PASSWORD or NAS_SSH_PRIVATE_KEY}"
  cat > "$ssh_dir/askpass" <<'ASKPASS'
#!/usr/bin/env bash
printf '%s\n' "$NAS_SSH_PASSWORD"
ASKPASS
  chmod 700 "$ssh_dir/askpass"
  export SSH_ASKPASS="$ssh_dir/askpass" SSH_ASKPASS_REQUIRE=force DISPLAY=github-actions
  options+=(-o PreferredAuthentications=password,keyboard-interactive -o PubkeyAuthentication=no -o BatchMode=no)
fi

# Password is the first encrypted stdin line, never an SSH command argument.
# The remote Bash variable is not exported to Docker/application processes.
printf -v remote_command "bash -c 'set +x; IFS= read -r DEPLOY_SUDO_PASSWORD; export -n DEPLOY_SUDO_PASSWORD; source /dev/stdin' -- %q %q %q" \
  "$NAS_PROJECT_DIR" "$EXPECTED_SHA" "$NAS_DOCKER_BIN"
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
{ printf '%s\n' "${NAS_SUDO_PASSWORD:-}"; cat "$script_dir/deploy_synology.sh"; } |
  ssh "${options[@]}" "$NAS_SSH_USER@$NAS_SSH_HOST" "$remote_command"
