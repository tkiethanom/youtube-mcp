import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import ValidationError
from shelf.store import Store
from shelf.youtube import AgeComparisonQuery, YouTube

VIDEO = 'abcdefghijk'
OTHER = 'lmnopqrstuv'
CHANNEL = 'owned-channel'


def video(video_id=VIDEO, published='2025-03-09T07:30:00Z', channel=CHANNEL):
    return {'id': video_id, 'snippet': {'channelId': channel, 'publishedAt': published,
            'title': 'Test video'}, 'statistics': {'viewCount': '123'}}


def response(data):
    return {'data': data, 'cached': False, 'fetched_at': '2025-03-20T12:00:00+00:00'}


class AgeComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'shelf.sqlite3'
        self.store = Store(self.path)
        self.youtube = YouTube(SimpleNamespace(channel_id=CHANNEL, data=self.path.parent), self.store)
        self.youtube.channel = Mock()
        self.youtube.request = Mock(return_value=response({'items': [video()]}))
        self.youtube.analytics = Mock(side_effect=self.report)

    def report(self, query, refresh):
        days = (query.end_date-query.start_date).days + 1
        records = [{'day': (query.start_date+timedelta(days=i)).isoformat(),
                    'views': 10, 'estimatedMinutesWatched': 2,
                    'subscribersGained': 1, 'subscribersLost': 0} for i in range(days)]
        return {**response({}), 'records': records}

    def test_reporting_day_and_dst_and_totals(self):
        result = self.youtube.compare_by_age([VIDEO], 3, True)['videos'][0]
        # 07:30 UTC is still March 8 in Los Angeles; DST changes March 9.
        self.assertEqual((result['start_date'], result['end_date']), ('2025-03-08', '2025-03-10'))
        self.assertEqual(result['status'], 'reported')
        self.assertEqual(result['totals']['views'], 30)
        self.assertEqual([r['age_day'] for r in result['daily']], [1, 2, 3])
        self.assertTrue(self.youtube.analytics.call_args.args[1])

    def test_each_video_uses_its_own_window(self):
        self.youtube.request.return_value = response({'items': [video(), video(OTHER, '2025-04-01T12:00:00Z')]})
        rows = self.youtube.compare_by_age([VIDEO, OTHER], 7)['videos']
        self.assertEqual(rows[0]['end_date'], '2025-03-14')
        self.assertEqual(rows[1]['start_date'], '2025-04-01')
        self.assertEqual(rows[1]['end_date'], '2025-04-07')

    def test_missing_day_is_unknown(self):
        report = self.report(SimpleNamespace(start_date=datetime(2025,3,8).date(), end_date=datetime(2025,3,10).date()), False)
        report['records'].pop(1)
        self.youtube.analytics.side_effect = None
        self.youtube.analytics.return_value = report
        row = self.youtube.compare_by_age([VIDEO], 3)['videos'][0]
        self.assertEqual(row['status'], 'incomplete_data')
        self.assertEqual(row['missing_days'], ['2025-03-09'])
        self.assertIsNone(row['totals'])

    def test_empty_report_is_not_zero(self):
        self.youtube.analytics.side_effect = None
        self.youtube.analytics.return_value = {**response({}), 'records': []}
        row = self.youtube.compare_by_age([VIDEO], 1)['videos'][0]
        self.assertIsNone(row['totals'])
        self.assertEqual(row['status'], 'incomplete_data')

    def test_young_video_not_queried(self):
        self.youtube.request.return_value = response({'items': [video(published=datetime.now(timezone.utc).isoformat())]})
        row = self.youtube.compare_by_age([VIDEO], 7)['videos'][0]
        self.assertEqual(row['status'], 'window_not_finished')
        self.youtube.analytics.assert_not_called()

    def test_wrong_channel_and_unavailable(self):
        self.youtube.request.return_value = response({'items': [video(channel='someone-else')]})
        rows = self.youtube.compare_by_age([VIDEO, OTHER])['videos']
        self.assertEqual([r['status'] for r in rows], ['wrong_channel', 'unavailable'])
        self.youtube.analytics.assert_not_called()

    def test_validation_before_network(self):
        for ids, age in [([], 7), ([VIDEO]*51, 7), (['bad'], 7), ([VIDEO], 0), ([VIDEO], 91), ([VIDEO], True), ([VIDEO], 1.5)]:
            with self.subTest(ids=ids[:1], age=age), self.assertRaises(ValidationError):
                self.youtube.compare_by_age(ids, age)
        self.youtube.channel.assert_not_called()
        self.assertEqual(AgeComparisonQuery(video_ids=[VIDEO,VIDEO]).video_ids, [VIDEO])

    def test_observations_persist_and_cache_expiry_does_not_delete_them(self):
        first = response({'items': [video()]})
        self.youtube.observe_videos(first)
        self.youtube.observe_videos(first)  # Same sample must not be duplicated.
        self.store.put('cache', 'expired', {}, -1)
        self.store.put('cache', 'next', {}, 900)
        self.store.clear_auth()
        reopened = Store(self.path)
        samples = reopened.observations_near(CHANNEL, VIDEO, 7*86400)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]['observed_at'], first['fetched_at'])
        self.assertEqual(reopened.observations_near('other-channel', VIDEO, 7*86400), [])
        with reopened.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM video_observations').fetchone()[0], 1)

    def test_nearest_samples_bracket_target_without_interpolation(self):
        for age in [1, 5, 7, 9]:
            self.store.save_observation(CHANNEL, VIDEO, {'observed_at': str(age), 'age_seconds': age, 'statistics': {'viewCount': str(age*10)}})
        samples = self.store.observations_near(CHANNEL, VIDEO, 6)
        self.assertEqual([s['age_seconds'] for s in samples], [5,7])

    def test_real_request_records_once_and_cached_read_keeps_timestamp(self):
        yt = YouTube(self.youtube.settings, self.store)
        credentials = Mock(valid=True, token='test-only')
        http = Mock(status_code=200)
        http.json.return_value = {'items': [video()]}
        (self.path.parent / 'youtube-token.json').write_text('{}')
        with patch('shelf.youtube.Credentials.from_authorized_user_file', return_value=credentials), patch('shelf.youtube.requests.get', return_value=http) as get:
            first = yt.request('data', 'videos', {'id': VIDEO})
            second = yt.request('data', 'videos', {'id': VIDEO})
        get.assert_called_once()
        self.assertTrue(second['cached'])
        self.assertEqual(first['fetched_at'], second['fetched_at'])
        samples = self.store.observations_near(CHANNEL, VIDEO, 7*86400)
        self.assertEqual(samples[0]['observed_at'], first['fetched_at'])

    def test_observation_offset_is_not_an_estimate(self):
        self.youtube.observe_videos(response({'items': [video()]}))
        row = self.youtube.compare_by_age([VIDEO], 7)['videos'][0]
        sample = row['observations'][0]
        self.assertEqual(sample['offset_from_target_seconds'], sample['age_seconds'] - 7*86400)
        self.assertEqual(sample['statistics']['viewCount'], '123')
        self.assertEqual(row['totals']['views'], 70)

    def test_missing_metric_keeps_totals_unknown(self):
        report = self.report(SimpleNamespace(start_date=datetime(2025,3,8).date(), end_date=datetime(2025,3,8).date()), False)
        del report['records'][0]['views']
        self.youtube.analytics.side_effect = None
        self.youtube.analytics.return_value = report
        row = self.youtube.compare_by_age([VIDEO], 1)['videos'][0]
        self.assertEqual(row['status'], 'incomplete_data')
        self.assertIsNone(row['totals'])

    def test_migrates_existing_database(self):
        legacy = self.path.parent / 'legacy.sqlite3'
        with sqlite3.connect(legacy) as db:
            db.execute('CREATE TABLE kv (kind TEXT, key TEXT, value TEXT, expires REAL, PRIMARY KEY(kind,key))')
        store = Store(legacy)
        store.save_observation(CHANNEL, VIDEO, {'observed_at':'now', 'age_seconds':1})
        self.assertEqual(len(store.observations_near(CHANNEL, VIDEO, 1)), 1)


class EndpointTests(unittest.TestCase):
    def test_http_auth_validation_and_mcp_dispatch(self):
        import os
        from unittest.mock import AsyncMock
        from fastapi.testclient import TestClient
        from shelf.app import create_app
        from shelf.config import Settings
        with tempfile.TemporaryDirectory() as d:
            settings = Settings(Path(d), 'https://youtube.example.com', '123', CHANNEL,
                                'test-client', 'test-secret', ['https://client.example.com/callback'])
            old_umask = os.umask(0o077)
            try:
                app = create_app(settings)
            finally:
                os.umask(old_umask)
            app.state.youtube.compare_by_age = Mock(return_value={'age_days':7, 'videos':[]})
            app.state.provider.load_access_token = AsyncMock(return_value=SimpleNamespace(scopes=['youtube:read']))
            with TestClient(app, base_url='https://youtube.example.com') as client:
                path = '/api/compare-videos-by-age'
                self.assertEqual(client.post(path, json={'video_ids':[VIDEO]}).status_code, 401)
                headers = {'Authorization':'Bearer test-token'}
                invalid = client.post(path, headers=headers, json={'video_ids':[VIDEO], 'age_days':0})
                self.assertEqual(invalid.status_code, 422)
                app.state.youtube.compare_by_age.assert_not_called()
                result = client.post(path+'?refresh=true', headers=headers, json={'video_ids':[VIDEO]})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()['age_days'], 7)
                app.state.youtube.compare_by_age.assert_called_once_with([VIDEO], 7, True)
            tools = asyncio.run(app.state.mcp.list_tools())
            self.assertIn('compare_videos_by_age', [t.name for t in tools])
            asyncio.run(app.state.mcp.call_tool('compare_videos_by_age', {'query':{'video_ids':[VIDEO], 'age_days':7}}))
            app.state.youtube.compare_by_age.assert_called_with([VIDEO], 7, False)


if __name__ == '__main__':
    unittest.main()
