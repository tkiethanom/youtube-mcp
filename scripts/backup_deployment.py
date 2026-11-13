"""Private NAS-local backup helper, executed in the service image as root."""
import os
import sqlite3
import sys
import tarfile
from pathlib import Path


def backup(data: Path, destination: Path):
    os.umask(0o077)
    for name in ('shelf.sqlite3', 'youtube-token.json', 'google-web-client.json'):
        if not (data / name).is_file():
            raise RuntimeError(f'Required persistent file is absent: {name}')
    archive = destination / 'data.tar.gz'
    # Run only after the service stops. Retain file modes, owners, credentials,
    # observations and SQLite sidecars in the private archive.
    with tarfile.open(archive, 'w:gz') as output:
        output.add(data, arcname='data')
    archive.chmod(0o600)
    with sqlite3.connect((data / 'shelf.sqlite3').as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(destination / 'shelf.sqlite3') as target:
            source.backup(target)
            if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('SQLite backup failed integrity verification')
    (destination / 'shelf.sqlite3').chmod(0o600)
    (destination / 'BACKUP_COMPLETE').write_text('Private data archive and SQLite backup verified.\n')
    print('Persistent data backup verified; contents were not printed.')


if __name__ == '__main__':
    backup(Path(sys.argv[1]), Path(sys.argv[2]))
