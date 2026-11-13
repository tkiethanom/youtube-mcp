import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = self.root / 'origin.git'
        self.seed = self.root / 'seed'
        self.nas = self.root / 'nas checkout'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.trace = self.root / 'docker-trace.jsonl'
        self.git('init', '--bare', str(self.origin), cwd=self.root)
        self.seed.mkdir()
        self.git('init', '-b', 'main', cwd=self.seed)
        self.git('config', 'user.name', 'Deployment test', cwd=self.seed)
        self.git('config', 'user.email', 'deployment-test@example.invalid', cwd=self.seed)
        (self.seed / '.gitignore').write_text('.env\ndata/\nbackups/\n')
        (self.seed / 'compose.yaml').write_text('services:\n  shelf:\n    image: fixture\n')
        (self.seed / 'version').write_text('old')
        self.git('add', '.', cwd=self.seed)
        self.git('commit', '-m', 'old', cwd=self.seed)
        self.old = self.git('rev-parse', 'HEAD', cwd=self.seed).stdout.strip()
        self.git('remote', 'add', 'origin', str(self.origin), cwd=self.seed)
        self.git('push', '-u', 'origin', 'main', cwd=self.seed)
        self.git('clone', '-b', 'main', str(self.origin), str(self.nas), cwd=self.root)
        (self.nas / '.env').write_text('PRIVATE_FIXTURE=do-not-print-this\n')
        (self.nas / 'data').mkdir()
        for name in ('youtube-token.json', 'google-web-client.json'):
            (self.nas / 'data' / name).write_text('{"fixture": "do-not-print-this"}')
        with sqlite3.connect(self.nas / 'data' / 'shelf.sqlite3') as db:
            db.execute('CREATE TABLE observations (value TEXT)')
            db.execute("INSERT INTO observations VALUES ('keep historical data')")
        (self.seed / 'scripts').mkdir()
        shutil.copy(ROOT / 'scripts' / 'backup_deployment.py', self.seed / 'scripts')
        (self.seed / 'version').write_text('new')
        self.git('add', '.', cwd=self.seed)
        self.git('commit', '-m', 'tested new code', cwd=self.seed)
        self.target = self.git('rev-parse', 'HEAD', cwd=self.seed).stdout.strip()
        self.git('push', 'origin', 'main', cwd=self.seed)
        self.write_executable('sudo', '''import os, sys
args = sys.argv[1:]
if args[:1] == ['-S']:
    password = sys.stdin.readline().rstrip('\\n')
    sys.exit(0 if password == 'fixture-password' else 1)
if args[:1] != ['-n']:
    sys.exit(2)
os.execv(args[1], args[1:])
''')
        self.write_executable('docker', '''import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
if os.environ.get('TEST_EXPECT_PRIVATE_VAR'):
    assert 'DEPLOY_SUDO_PASSWORD' not in os.environ
with open(os.environ['TEST_TRACE'], 'a') as file:
    file.write(json.dumps(args) + '\\n')
phase = os.environ.get('TEST_FAIL_PHASE', '')
if args[:1] == ['inspect']:
    print('sha256:previous-image' if '.Image' in args[2] else 'fixture-project'
          if 'com.docker.compose.project' in args[2] else ('unhealthy' if phase == 'health' else 'healthy'))
    sys.exit(0)
if args[:1] == ['image']:
    sys.exit(0)
if args[:1] != ['compose']:
    sys.exit(2)
command = next((a for a in args[1:] if a in ('version', 'config', 'ps', 'build', 'stop', 'run', 'up')), '')
if command == 'ps':
    print('fixture-container')
if command == 'build' and phase == 'build':
    sys.exit(1)
if command == 'run':
    if phase == 'backup':
        sys.exit(1)
    project = Path(args[args.index('--project-directory') + 1])
    mounts = [args[i+1] for i, arg in enumerate(args) if arg == '--volume']
    destination = next(m.split(':/backup:')[0] for m in mounts if ':/backup:' in m)
    subprocess.run([sys.executable, str(project / 'scripts/backup_deployment.py'), str(project / 'data'), destination], check=True)
''')
        self.write_executable('curl', 'import sys\nsys.exit(0)\n')
        self.write_executable('sleep', 'import sys\nsys.exit(0)\n')
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        DEPLOY_SUDO_PASSWORD='fixture-password', TEST_TRACE=str(self.trace))

    def git(self, *args, cwd):
        return subprocess.run(['git', *args], cwd=cwd, check=True, text=True, capture_output=True)

    def write_executable(self, name, body):
        path = self.bin / name
        path.write_text('#!' + sys.executable + '\n' + body)
        path.chmod(0o700)
        return path

    def deploy(self, phase=''):
        env = self.env | {'TEST_FAIL_PHASE': phase}
        result = subprocess.run(['bash', str(ROOT / 'scripts/deploy_synology.sh'), str(self.nas),
                                 self.target, str(self.bin / 'docker')], env=env,
                                text=True, capture_output=True, timeout=30)
        self.assertNotIn('fixture-password', result.stdout + result.stderr)
        self.assertNotIn('do-not-print-this', result.stdout + result.stderr)
        return result

    def commands(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def test_success_backs_up_private_data_and_deploys_exact_tested_commit(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.nas).stdout.strip(), self.target)
        backup = next((self.nas / 'backups').glob('deploy-*'))
        self.assertTrue((backup / 'BACKUP_COMPLETE').exists())
        self.assertEqual((backup / '.env').read_text(), (self.nas / '.env').read_text())
        self.assertEqual((backup / 'data.tar.gz').stat().st_mode & 0o777, 0o600)
        with tarfile.open(backup / 'data.tar.gz') as archive:
            self.assertIn('data/youtube-token.json', archive.getnames())
        with sqlite3.connect(backup / 'shelf.sqlite3') as db:
            self.assertEqual(db.execute('SELECT value FROM observations').fetchone()[0], 'keep historical data')
        commands = self.commands()
        build = next(i for i,c in enumerate(commands) if 'build' in c)
        stop = next(i for i,c in enumerate(commands) if 'stop' in c)
        run = next(i for i,c in enumerate(commands) if 'run' in c)
        up = next(i for i,c in enumerate(commands) if 'up' in c)
        self.assertLess(build, stop)
        self.assertLess(stop, run)
        self.assertLess(run, up)
        self.assertFalse((self.nas / 'backups/.deploy-lock').exists())

    def test_failed_build_keeps_service_running_and_restores_checkout(self):
        result = self.deploy('build')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.nas).stdout.strip(), self.old)
        self.assertFalse(any('stop' in c or 'up' in c for c in self.commands()))

    def test_failed_backup_restarts_old_image_without_erasing_data(self):
        result = self.deploy('backup')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.nas).stdout.strip(), self.old)
        rollback = [c for c in self.commands() if 'up' in c]
        self.assertEqual(len(rollback), 1)
        self.assertTrue(any('rollback-compose.json' in a for a in rollback[0]))
        self.assertEqual((self.nas / '.env').read_text(), 'PRIVATE_FIXTURE=do-not-print-this\n')

    def test_failed_health_check_attempts_old_image_and_keeps_database(self):
        result = self.deploy('health')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.nas).stdout.strip(), self.old)
        self.assertEqual(len([c for c in self.commands() if 'up' in c]), 2)
        with sqlite3.connect(self.nas / 'data/shelf.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM observations').fetchone()[0], 1)

    def test_newer_main_or_local_changes_are_not_deployed(self):
        (self.seed / 'version').write_text('newer untested code')
        self.git('add', '.', cwd=self.seed)
        self.git('commit', '-m', 'newer', cwd=self.seed)
        self.git('push', 'origin', 'main', cwd=self.seed)
        result = self.deploy()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('main has moved', result.stderr)
        self.assertFalse(any('build' in c or 'stop' in c for c in self.commands()))
        (self.nas / 'version').write_text('local edits')
        result = self.deploy()
        self.assertIn('local changes', result.stderr)
        self.assertEqual((self.nas / 'version').read_text(), 'local edits')

    def test_ssh_wrapper_keeps_password_out_of_arguments_and_cleans_files(self):
        self.write_executable('ssh', '''import os, subprocess, sys
assert 'fixture-password' not in ' '.join(sys.argv)
assert '/dev/stdin' not in sys.argv[-1]
assert 'StrictHostKeyChecking=yes' in sys.argv
assert os.environ['SSH_ASKPASS_REQUIRE'] == 'force'
assert subprocess.check_output([os.environ['SSH_ASKPASS']], text=True).strip() == 'fixture-password'
password = sys.stdin.readline().rstrip('\\n')
assert password == 'fixture-password'
script = sys.stdin.read()
assert 'compose build shelf' in script
remote_env = {k:v for k,v in os.environ.items() if not k.startswith('NAS_') and k != 'DEPLOY_SUDO_PASSWORD'}
remote_env['TEST_EXPECT_PRIVATE_VAR'] = '1'
result = subprocess.run(['bash', '-c', sys.argv[-1]], input=password+'\\n'+script,
                        env=remote_env, text=True, capture_output=True)
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
sys.exit(result.returncode)
''')
        env = self.env | {'NAS_SSH_HOST': 'nas.example.invalid', 'NAS_SSH_USER': 'tawin',
            'NAS_SSH_PORT': '2222', 'NAS_PROJECT_DIR': str(self.nas),
            'NAS_DOCKER_BIN': str(self.bin / 'docker'), 'NAS_SSH_KNOWN_HOSTS': 'fixture-public-host-key',
            'NAS_SSH_PASSWORD': 'fixture-password', 'NAS_SUDO_PASSWORD': 'fixture-password',
            'NAS_SSH_PRIVATE_KEY': '', 'EXPECTED_SHA': self.target, 'RUNNER_TEMP': str(self.root)}
        result = subprocess.run(['bash', str(ROOT / 'scripts/deploy_over_ssh.sh')], env=env,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('fixture-password', result.stdout + result.stderr)
        self.assertEqual(list(self.root.glob('shelf-ssh.*')), [])
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=self.nas).stdout.strip(), self.target)
