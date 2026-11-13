# GitHub Actions deployment to Synology

The `Test and deploy to Synology` workflow runs tests on pull requests and pushes
to `main`. After successful tests on `main`, it deploys over SSH. It can also be
started from Actions → Test and deploy to Synology → Run workflow, choosing main.
Pull requests and other branches cannot enter the deployment job.

No GitHub-hosted runner needs Docker access locally. Docker Compose builds and
runs the application on the NAS. The existing persistent `./data:/data` mount,
OAuth files and database stay on the NAS. The pipeline does not copy them to
GitHub, publish an image, or upload backup artifacts. Backups are also excluded
from Docker's build context.

## GitHub settings

In the repository, open Settings → Secrets and variables → Actions.

Required **secrets**:

| Name | Value |
| --- | --- |
| `NAS_SSH_PASSWORD` | The NAS account login password, if using password authentication |
| `NAS_SSH_KNOWN_HOSTS` | A verified OpenSSH known_hosts entry for the NAS hostname and SSH port |

Optional **secrets**:

| Name | Value |
| --- | --- |
| `NAS_SUDO_PASSWORD` | Sudo password if it differs from the SSH password; otherwise the workflow reuses NAS_SSH_PASSWORD |
| `NAS_SSH_PRIVATE_KEY` | Optional dedicated unencrypted deployment SSH private key, used instead of password-based SSH if set |

Required **variable**:

| Name | Value |
| --- | --- |
| `NAS_SSH_HOST` | The externally reachable NAS SSH hostname/IP you provided |

Optional variables (defaults match the supplied NAS setup):

| Name | Default |
| --- | --- |
| `NAS_SSH_USER` | `tawin` |
| `NAS_SSH_PORT` | `2222` |
| `NAS_PROJECT_DIR` | `/volume1/docker/youtube-mcp-git` |
| `NAS_DOCKER_BIN` | `/usr/local/bin/docker` |

These may be repository settings or settings on the workflow's `nas` GitHub
environment. Environment secrets/variables override repository values. The job
references `environment: nas`, so any protection rules configured there apply.
No new required approval rule is introduced by the workflow.

Do not put passwords, keys or OAuth credentials in YAML, Git, chat, or workflow
inputs. Enter secret values only through GitHub's secret settings. Values are
passed as environment data, not interpolated into shell source.

### Host-key verification

The workflow requires strict host-key verification. From a trusted existing NAS
terminal/console, obtain its **public** SSH host key. For port 2222, this produces
the entry to place in `NAS_SSH_KNOWN_HOSTS`; substitute the reachable hostname:

```bash
printf '[YOUR_NAS_HOST]:2222 '
cat /etc/ssh/ssh_host_ed25519_key.pub
```

Use only the `.pub` file. If the NAS uses another host-key type, use its matching
public key. Obtain/verify this key through your trusted NAS connection. The
pipeline never disables host checks or blindly trusts a runtime key scan.

### Password-based SSH and sudo

The runner uses OpenSSH's askpass mechanism for password-based SSH, so no extra
sshpass package or third-party SSH action is needed. If an SSH key is supplied,
the key is kept in a temporary mode-600 file and password-based SSH is disabled.

The sudo password goes through the encrypted SSH stdin stream into an unexported
remote Bash variable. Before each Docker operation, `sudo -S -v` refreshes the
existing sudo authorization, followed by `sudo -n /absolute/path/to/docker ...`.
The password does not become a command-line argument or a container environment
variable. Tracing is disabled, and temporary runner credential files are deleted
on exit. Passwords containing a newline are unsupported.

If the same password is used for SSH and sudo, only NAS_SSH_PASSWORD is necessary.
There is no required sudoers change. If you later use an SSH key and already have
password-free sudo for Docker, both password secrets can be omitted. Docker/sudo
deployment access grants extensive control of the NAS; keep the deployment
credential limited to this repository/environment and trusted maintainers.

## NAS prerequisites

- The project is an existing clean checkout on branch `main`, with its `.env`,
  `data` directory and one existing `shelf` Compose container.
- The SSH account can read `.env`, write the Git checkout/backups, and run
  `sudo docker`. Persistent data files can remain owned by the container UID:
  the backup helper reads them as root in a one-off container.
- `bash`, `git`, `curl`, `sudo` and Docker Compose v2 are installed. If
  `command -v docker` gives another path, set NAS_DOCKER_BIN accordingly.
- The account's GitHub fetch works noninteractively using its existing Git
  authentication. The NAS-to-GitHub credential is separate from Actions-to-NAS
  SSH authentication. This workflow does not change the NAS Git credentials.
- There is space for an old/new image and private persistent-data backups.
  Keep any previous competing Compose project stopped.

## Deployment sequence

1. On an Ubuntu GitHub runner, install the repository's pinned Python 3.12
   dependencies and run the complete test suite, dependency checks and Bash
   syntax checks. The checkout/setup actions are pinned to verified commit SHAs.
2. Connect over SSH to the pinned NAS host. Reject invalid parameters, a dirty
   checkout, another branch, or a missing existing service. Acquire an exclusive
   NAS deployment lock; GitHub also serializes deployments without cancelling an
   in-progress deployment for a newer push.
3. Fetch main, confirm it still matches the tested SHA, and fast-forward the NAS
   checkout to that exact SHA. This is the fetch/fast-forward equivalent of
   `git pull --ff-only origin main`, with an explicit tested-commit check. A newer
   main present at this point causes a clean refusal; rerun for its tests.
4. Preserve the previous image under a rollback tag and build `shelf` while the
   existing container keeps serving requests.
5. Save the previous Compose configuration, `.env` and commit IDs under a
   private `backups/deploy-...` directory. Stop `shelf`, archive the complete
   persistent data directory, and create/check a separate SQLite backup. OAuth
   files and historical observations are included; contents are never printed.
6. Recreate `shelf` from the built image, then require Docker's health check and
   the local HTTP `/healthz` endpoint to pass. Allow approximately three minutes
   plus probe/command time. Record the deployed commit on success.

The pipeline builds only the `shelf` service. It does not run `compose down -v`,
delete data, prune old images/backups, change OAuth scopes, or create Google bulk
reporting jobs. Once deployed, the existing explicit bulk setup tool/CLI can
create or reuse the already-authorized traffic-source/reach jobs separately.

## Failures and recovery

A build failure leaves the existing container running. If a backup or startup
step fails after stopping the service, the script attempts to restore the
previous image with the saved Compose configuration. It restores the previous
Git revision only if no local edits or intervening commit appeared.

The database is **not automatically restored from backup**: restoring an older
snapshot could erase observations collected during the failed deployment. The
application currently uses additive migrations compatible with the previous
version. Destructive migrations need a separate reviewed deployment procedure.
An automatic recovery error is reported prominently and needs NAS inspection.

Private backup files may be owned by root; use the NAS sudo account to inspect
or restore them. Keep `data.tar.gz`, the verified `shelf.sqlite3`, `.env`, commit
records and rollback image tag together. Recovery never uploads these files.
Do not prune rollback images/backups until the deployed service is verified.

If a runner is forcibly killed, a NAS deployment lock may remain at
`backups/.deploy-lock`. Confirm no deployment is running before removing that
empty directory and rerunning. A cancelled workflow cannot guarantee recovery;
this is why newer pushes do not cancel an in-progress deployment.

## Verification performed

Local deployment simulations use temporary Git repositories, real SQLite/archive
backups, and mocked SSH/sudo/Docker/curl operations. They cover successful exact-
commit deployment, build failure, backup failure, failed health checks, newer-main
and dirty-checkout refusal, password isolation, quoted paths, and cleanup.
They do not contact the production NAS. All 70 tests, dependency consistency,
Actionlint and both Bash syntax checks pass. Real SSH reachability, host keys, credentials, sudo and Docker must be
confirmed by the first configured workflow run.
