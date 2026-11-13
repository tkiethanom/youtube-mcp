"""Run explicitly requested bulk operations inside the configured container."""
import argparse
import json

from .bulk import BulkReports, BulkSetupQuery, REPORT_TYPES
from .config import Settings
from .store import Store
from .youtube import YouTube


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    setup = commands.add_parser('setup', help='Create/reuse daily traffic-source and reach-basic jobs')
    setup.add_argument('--report-type', action='append', choices=REPORT_TYPES)
    sync = commands.add_parser('import', help='Download one page of generated CSV reports on demand')
    sync.add_argument('--job-id', required=True)
    sync.add_argument('--max-reports', type=int, default=10)
    sync.add_argument('--page-token', default='')
    args = parser.parse_args()
    settings = Settings.from_env()
    bulk = BulkReports(YouTube(settings, Store(settings.data / 'shelf.sqlite3')))
    if args.action == 'setup':
        result = bulk.setup(BulkSetupQuery(report_types=args.report_type or list(REPORT_TYPES)))
    else:
        result = bulk.sync(args.job_id, args.max_reports, args.page_token)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
