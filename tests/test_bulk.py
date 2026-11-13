import io
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from shelf.bulk import BulkReports, BulkSetupQuery, REACH_REPORT, TRAFFIC_REPORT, timestamp
from shelf.store import Store
from shelf.youtube import GoogleAPIError


HEADER = 'date,channel_id,video_id,traffic_source_type,traffic_source_detail,views,watch_time_minutes,engaged_views\n'
CSV = HEADER + '20250101,owned,abcdefghijk,7,referrer001,3,5.5,2\n'


def metadata(report_id='report1', created='2025-01-03T12:00:00Z'):
    return {'id': report_id, 'jobId': 'job1', 'startTime': '2025-01-01T08:00:00Z',
            'endTime': '2025-01-02T08:00:00Z', 'createTime': created,
            'downloadUrl': 'https://youtubereporting.googleapis.com/v1/media/test'}


class BulkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'test.sqlite3')
        self.yt = SimpleNamespace(store=self.store, settings=SimpleNamespace(channel_id='owned'),
                                  lock=threading.RLock(), channel=Mock(), request=Mock(),
                                  credentials=Mock(return_value=Mock(token='test-only')))
        self.bulk = BulkReports(self.yt)
        self.job = {'id': 'job1', 'reportTypeId': TRAFFIC_REPORT}

    def import_csv(self, csv=CSV, meta=None):
        response = Mock(status_code=200)
        response.iter_content.return_value = [csv.encode()]
        meta = meta or metadata()
        with patch('shelf.bulk.requests.get', return_value=response):
            return self.bulk.import_report('job1', meta['id'], job=self.job, metadata=meta)

    def test_revisions_are_preserved_but_not_double_counted(self):
        self.assertEqual(self.import_csv()['rows'], 1)
        self.import_csv(CSV.replace(',3,5.5,2', ',4,6.5,3'), metadata('revision2', '2025-01-04T12:00:00Z'))
        self.import_csv(CSV.replace(',3,5.5,2', ',2,4.5,1'), metadata('older-imported-later', '2025-01-02T12:00:00Z'))
        selected = self.bulk.select(TRAFFIC_REPORT, date(2025,1,1), date(2025,1,2))
        self.assertEqual(selected['report_ids'], ['revision2'])
        self.assertEqual(selected['missing_report_days'], ['2025-01-02'])
        rows = list(self.bulk.rows(selected, date(2025,1,1), date(2025,1,2), ['abcdefghijk']))
        self.assertEqual(rows[0]['watch_time_minutes'], '6.5')
        self.store.clear_auth()
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM bulk_reports').fetchone()[0], 3)
        with patch('shelf.bulk.requests.get') as get:
            result = self.bulk.import_report('job1', 'report1', job=self.job, metadata=metadata())
            self.assertEqual(result['status'], 'already_imported')
            get.assert_not_called()

    def test_invalid_csv_rolls_back_all_rows(self):
        for csv in [HEADER + '20250101,owned,abcdefghijk,7,referrer001,3,5.5,2\n20250101,wrong-channel,abcdefghijk,7,x,1,2,1\n',
                    'date,video_id\n20250101,abcdefghijk\n',
                    CSV + '20250101,owned,too-few-columns\n']:
            with self.subTest(csv=csv), self.assertRaises(ValueError):
                self.import_csv(csv)
            with self.store.db() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM bulk_rows').fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT count(*) FROM bulk_reports').fetchone()[0], 0)

    def test_download_url_does_not_leak_auth_to_other_hosts(self):
        for url in ['https://attacker.example/v1/media/test',
                    'http://youtubereporting.googleapis.com/v1/media/test',
                    'https://user@youtubereporting.googleapis.com/v1/media/test',
                    'https://youtubereporting.googleapis.com/v1/jobs']:
            with self.subTest(url=url), patch('shelf.bulk.requests.get') as get:
                with self.assertRaises(ValueError):
                    self.bulk.import_report('job1', 'report1', job=self.job, metadata=metadata() | {'downloadUrl': url})
                get.assert_not_called()

    def test_empty_csv_retains_report_coverage_without_inventing_rows(self):
        self.import_csv(HEADER)
        selection = self.bulk.select(TRAFFIC_REPORT, date(2025,1,1), date(2025,1,1))
        self.assertEqual(selection['missing_report_days'], [])
        self.assertEqual(list(self.bulk.rows(selection, date(2025,1,1), date(2025,1,1), ['abcdefghijk'])), [])

    def test_oversized_download_does_not_import_partial_data(self):
        with patch('shelf.bulk.MAX_DOWNLOAD_BYTES', 10), self.assertRaisesRegex(ValueError, 'import limit'):
            self.import_csv()
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM bulk_reports').fetchone()[0], 0)

    def test_setup_reuses_existing_jobs_and_creates_only_missing_type(self):
        self.yt.request.side_effect = [
            {'data': {'reportTypes': [{'id': TRAFFIC_REPORT}, {'id': REACH_REPORT}]}},
            {'data': {'jobs': [self.job]}}]
        response = Mock(status_code=200)
        response.json.return_value = {'id': 'reach-job', 'reportTypeId': REACH_REPORT}
        with patch('shelf.bulk.requests.post', return_value=response) as post:
            result = self.bulk.setup(BulkSetupQuery())
        self.assertEqual([r['status'] for r in result['jobs']], ['existing', 'created'])
        self.assertEqual(post.call_args.kwargs['json']['reportTypeId'], REACH_REPORT)
        self.assertFalse(post.call_args.kwargs['allow_redirects'])

    def test_unfinished_job_listing_never_creates_duplicate_jobs(self):
        self.yt.request.side_effect = [
            {'data': {'reportTypes': [{'id': TRAFFIC_REPORT}]}},
            {'data': {'jobs': [], 'nextPageToken': 'same'}},
            {'data': {'jobs': [], 'nextPageToken': 'same'}}]
        with patch('shelf.bulk.requests.post') as post, self.assertRaises(GoogleAPIError):
            self.bulk.setup(BulkSetupQuery(report_types=[TRAFFIC_REPORT]))
        post.assert_not_called()

    def test_ambiguous_creation_response_instructs_listing_before_retry(self):
        self.yt.request.side_effect = [
            {'data': {'reportTypes': [{'id': TRAFFIC_REPORT}]}}, {'data': {'jobs': []}}]
        response = Mock(status_code=200)
        response.json.side_effect = ValueError('invalid JSON')
        with patch('shelf.bulk.requests.post', return_value=response), self.assertRaisesRegex(
                GoogleAPIError, 'List existing jobs before retrying'):
            self.bulk.setup(BulkSetupQuery(report_types=[TRAFFIC_REPORT]))

    def test_timezone_offsets_are_normalized_before_revision_selection(self):
        self.assertEqual(timestamp('2025-01-01T08:00:00Z'), timestamp('2025-01-01T00:00:00-08:00'))

    def test_sync_returns_partial_failure_and_next_token(self):
        self.yt.request.side_effect = [
            {'data': self.job},
            {'data': {'reports': [metadata(), metadata('report2')], 'nextPageToken': 'next'},
             'fetched_at': 'sample-time'}]
        with patch.object(self.bulk, 'import_report', side_effect=[
                {'report_id': 'report1', 'status': 'imported'}, ValueError('Invalid CSV')]):
            result = self.bulk.sync('job1', max_reports=2)
        self.assertEqual(result['status'], 'partial_failure')
        self.assertEqual(result['next_page_token'], 'next')
        self.assertEqual([r['status'] for r in result['reports']], ['imported', 'failed'])

    def test_reach_csv_import_preserves_daily_ctr(self):
        self.job['reportTypeId'] = REACH_REPORT
        csv = 'date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n20250101,owned,abcdefghijk,100,3.5\n'
        self.import_csv(csv)
        selection = self.bulk.select(REACH_REPORT, date(2025,1,1), date(2025,1,1))
        row = list(self.bulk.rows(selection, date(2025,1,1), date(2025,1,1), ['abcdefghijk']))[0]
        self.assertEqual(row['video_thumbnail_impressions_ctr'], '3.5')
