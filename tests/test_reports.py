import asyncio
import io
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
from pydantic import ValidationError

from shelf.app import create_app
from shelf.bulk import BulkReports, TRAFFIC_REPORT, timestamp
from shelf.config import Settings
from shelf.reports import ChannelReports, DiscoveryQuery, SuggestedSourcesQuery, VideoPerformanceQuery
from shelf.store import Store
from shelf.youtube import GoogleAPIError

VIDEO = 'abcdefghijk'
OTHER = 'lmnopqrstuv'
NEW = 'newvideo001'
REF = 'referrer001'
REF2 = 'referrer002'
OWNED = 'UC' + 'o' * 22
CREATOR = 'UC' + 'c' * 22


def metadata(vid, channel=OWNED, published='2024-01-01T12:00:00Z'):
    return {'id': vid, 'snippet': {'channelId': channel, 'title': 'Title ' + vid,
            'channelTitle': 'Channel ' + channel, 'publishedAt': published}, 'statistics': {'viewCount': '100'}}


def envelope(data):
    return {'data': data, 'fetched_at': '2026-11-13T12:00:00+00:00', 'cached': False}


def suggested(**changes):
    return SuggestedSourcesQuery(**({'video_ids': [VIDEO, OTHER], 'date_ranges': [
        {'label': 'before', 'start_date': '2025-01-01', 'end_date': '2025-01-02'},
        {'label': 'after', 'start_date': '2025-02-01', 'end_date': '2025-02-02'}]} | changes))


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'test.sqlite3')
        self.yt = SimpleNamespace(store=self.store, settings=SimpleNamespace(channel_id=OWNED),
                                  channel=Mock(), request=Mock(side_effect=self.request),
                                  analytics=Mock(side_effect=self.analytics))
        self.items = {VIDEO: metadata(VIDEO), OTHER: metadata(OTHER),
                      NEW: metadata(NEW, published='2025-03-09T07:30:00Z'),
                      REF: metadata(REF, CREATOR), REF2: metadata(REF2, 'different-channel')}
        self.reports = ChannelReports(self.yt)

    def request(self, service, path, params, refresh=False):
        if path == 'videos':
            return envelope({'items': [self.items[v] for v in params['id'].split(',') if v in self.items]})
        if path == 'channels':
            return envelope({'items': [{'id': CREATOR, 'snippet': {'title': 'A creator', 'customUrl': '@SomeCreator'}}]})
        if path == 'reportTypes':
            return envelope({'reportTypes': [{'id': 'channel_reach_basic_a1'}]})
        raise AssertionError(path)

    def analytics(self, query, refresh=False):
        if query.dimensions == 'insightTrafficSourceDetail':
            scale = 1 if query.start_date.month == 1 else 2
            records = [{'insightTrafficSourceDetail': REF, 'views': 10 * scale, 'estimatedMinutesWatched': 20 * scale},
                       {'insightTrafficSourceDetail': REF2, 'views': 5, 'estimatedMinutesWatched': 3}]
        elif query.dimensions == 'day':
            records = [{'day': (query.start_date+timedelta(days=i)).isoformat(),
                        **{m: 10 for m in query.metrics.split(',')}}
                       for i in range((query.end_date-query.start_date).days+1)]
            records = records[query.start_index-1:query.start_index-1+query.max_results]
        else:
            records = [{m: 10 for m in query.metrics.split(',')}]
        return {**envelope({}), 'records': records,
                'next_start_index': query.start_index+len(records) if len(records) == query.max_results else None}

    def test_optional_at_handle_and_channel_id_resolve_to_stable_identity(self):
        for creator in ['SomeCreator', '@SomeCreator', CREATOR]:
            result = self.reports.resolve_creator(creator)
            self.assertEqual(result['channel_id'], CREATOR)
            sent = self.yt.request.call_args.args[2]
            self.assertEqual(sent.get('forHandle', sent.get('id')), 'SomeCreator' if creator != CREATOR else CREATOR)
        with self.assertRaises(ValueError):
            self.reports.resolve_creator('https://youtube.com/@SomeCreator')

    def test_referrals_compare_windows_without_summing_overlapping_ranges(self):
        result = self.reports.suggested(suggested(creator='@SomeCreator'), True)
        before, after = result['ranges']
        self.assertEqual(before['by_referring_video'][0]['views'], 20)
        self.assertEqual(after['by_referring_channel'][0]['views'], 40)
        self.assertEqual(result['comparisons'][0]['sources'][0]['visible_views_delta'], 20)
        self.assertIsNone(before['by_referring_video'][0]['average_duration_seconds'])
        self.assertTrue(before['creator_filter_metadata_complete'])
        for call in self.yt.analytics.call_args_list:
            query = call.args[0]
            self.assertEqual(query.metrics, 'views,estimatedMinutesWatched')
            self.assertEqual(query.max_results, 25)
            self.assertEqual(query.start_index, 1)
            self.assertIn('insightTrafficSourceType==RELATED_VIDEO', query.filters)
            self.assertNotIn(',', query.filters)

    def test_missing_metadata_and_resolution_limit_do_not_silently_claim_complete_filter(self):
        result = self.reports.suggested(suggested(creator='SomeCreator', max_metadata_videos=1))
        self.assertEqual(result['metadata_resolution']['candidate_count'], 2)
        self.assertEqual(result['metadata_resolution']['unresolved_video_ids'], [REF2])
        self.assertFalse(result['ranges'][0]['creator_filter_metadata_complete'])
        unresolved = result['ranges'][0]['uploads'][0]['unresolved_sources_excluded_from_creator_filter']
        self.assertEqual(unresolved[0]['referring_video_id'], REF2)

    def test_missing_source_in_comparison_stays_unknown_not_zero(self):
        original = self.analytics
        def report(query, refresh=False):
            result = original(query, refresh)
            if query.start_date.month == 2:
                result['records'].pop()
            return result
        self.yt.analytics.side_effect = report
        result = self.reports.suggested(suggested())
        deltas = {r['referring_video_id']: r['visible_views_delta'] for r in result['comparisons'][0]['sources']}
        self.assertIsNone(deltas[REF2])

    def test_wrong_channel_and_unavailable_videos_are_not_queried(self):
        self.items[OTHER] = metadata(OTHER, 'not-owned')
        del self.items[VIDEO]
        result = self.reports.suggested(suggested())
        self.assertEqual([u['status'] for u in result['ranges'][0]['uploads']], ['unavailable', 'wrong_channel'])
        self.yt.analytics.assert_not_called()

    def test_metadata_batches_never_exceed_50(self):
        self.yt.request.reset_mock()
        self.reports._metadata([str(i).zfill(11) for i in range(102)])
        self.assertEqual(len(self.yt.request.call_args_list), 3)
        self.assertEqual([len(c.args[2]['id'].split(',')) for c in self.yt.request.call_args_list], [50, 50, 2])

    def test_bulk_referrals_preserve_unknown_identity_and_exclude_other_sources(self):
        header = 'date,channel_id,video_id,traffic_source_type,traffic_source_detail,views,watch_time_minutes\n'
        csv = header + f'20250101,{OWNED},{VIDEO},7,{REF},3,5.5\n20250101,{OWNED},{VIDEO},7,,2,1\n20250101,{OWNED},{VIDEO},5,search term,100,20\n'
        meta = {'id': 'bulk1', 'startTime': '2025-01-01T08:00:00Z', 'endTime': '2025-01-02T08:00:00Z', 'createTime': '2025-01-03T12:00:00Z'}
        bulk = BulkReports(self.yt)
        bulk._import_csv(io.StringIO(csv), OWNED, 'bulk1', 'job1', TRAFFIC_REPORT, meta,
                         'fixture-hash', timestamp(meta['startTime']), timestamp(meta['endTime']), timestamp(meta['createTime']))
        result = self.reports.suggested(suggested(video_ids=[VIDEO], source='bulk', date_ranges=[
            {'label': 'window', 'start_date': '2025-01-01', 'end_date': '2025-01-02'}]))
        window = result['ranges'][0]
        self.assertEqual(sum(r['views'] for r in window['by_referring_video']), 5)
        self.assertEqual(window['uploads'][0]['status'], 'incomplete_data')
        self.assertTrue(any(r['referring_video_id'] is None for r in window['by_referring_video']))
        self.yt.analytics.assert_not_called()

    def test_unified_report_preserves_successful_sections_when_one_fails(self):
        original = self.analytics
        def report(query, refresh=False):
            if query.dimensions == 'insightTrafficSourceType':
                raise GoogleAPIError(403, 'quota', reasons=['quotaExceeded'])
            return original(query, refresh)
        self.yt.analytics.side_effect = report
        result = self.reports.performance(VideoPerformanceQuery(video_ids=[VIDEO], start_date='2025-01-01', end_date='2025-01-02'))
        video = result['videos'][0]
        self.assertEqual(video['status'], 'partial_data')
        self.assertEqual(video['summary']['status'], 'reported')
        self.assertEqual(video['traffic_sources']['error']['category'], 'quota')
        self.assertEqual(video['reach']['status'], 'not_imported_or_no_rows')

    def test_default_discovery_windows_include_upload_day_with_pacific_timezone(self):
        result = self.reports.discovery(DiscoveryQuery(new_video_id=NEW, older_video_ids=[VIDEO, NEW]))
        self.assertEqual(result['publication_reporting_day'], '2025-03-08')
        row = result['videos'][0]
        self.assertEqual((row['before']['start_date'], row['before']['end_date']), ('2025-02-08', '2025-03-07'))
        self.assertEqual((row['after']['start_date'], row['after']['end_date']), ('2025-03-08', '2025-04-04'))
        self.assertEqual(row['before']['totals']['views'], 280)
        self.assertEqual(row['change']['views']['per_day_delta'], 0)
        self.assertEqual(result['videos'][1]['status'], 'not_an_older_video')
        self.assertIn('correlation', result['note'])

    def test_discovery_missing_day_keeps_deltas_unknown(self):
        original = self.analytics
        def report(query, refresh=False):
            result = original(query, refresh)
            result['records'].pop()
            return result
        self.yt.analytics.side_effect = report
        row = self.reports.discovery(DiscoveryQuery(new_video_id=NEW, older_video_ids=[VIDEO]))['videos'][0]
        self.assertIsNone(row['before']['totals'])
        self.assertIsNone(row['change'])

    def test_custom_discovery_windows_normalize_days_and_zero_baseline(self):
        original = self.analytics
        def report(query, refresh=False):
            result = original(query, refresh)
            if query.end_date < date(2025,3,8):
                for row in result['records']:
                    row['views'] = 0
            return result
        self.yt.analytics.side_effect = report
        query = DiscoveryQuery(new_video_id=NEW, older_video_ids=[VIDEO],
            before={'start_date': '2025-03-06', 'end_date': '2025-03-07'},
            after={'start_date': '2025-03-08', 'end_date': '2025-03-11'})
        row = self.reports.discovery(query)['videos'][0]
        self.assertIsNone(row['change']['views']['per_day_percent_change'])
        self.assertEqual(row['change']['views']['per_day_delta'], 10)

    def test_daily_pagination_preserves_more_than_200_reporting_days(self):
        result = self.reports._daily(VIDEO, date(2025,1,1), date(2025,8,1), 'views', False)
        self.assertEqual(len(result['records']), 213)
        self.assertEqual(result['status'], 'reported')
        self.assertEqual([c.args[0].start_index for c in self.yt.analytics.call_args_list], [1, 201])

    def test_exact_200_day_window_does_not_make_an_extra_page_request(self):
        result = self.reports._daily(VIDEO, date(2025,1,1), date(2025,7,19), 'views', False)
        self.assertEqual(len(result['records']), 200)
        self.assertEqual(result['status'], 'reported')
        self.yt.analytics.assert_called_once()

    def test_metadata_failure_retains_referring_ids_and_metrics(self):
        original = self.request
        def request(service, path, params, refresh=False):
            if path == 'videos' and REF in params['id']:
                raise GoogleAPIError(503, 'Metadata service unavailable', source='data')
            return original(service, path, params, refresh)
        self.yt.request.side_effect = request
        result = self.reports.suggested(suggested())
        self.assertEqual(result['metadata_resolution']['error']['category'], 'upstream')
        rows = result['ranges'][0]['by_referring_video']
        self.assertEqual(rows[0]['referring_video_id'], REF)
        self.assertEqual(rows[0]['views'], 20)
        self.assertIsNone(rows[0]['referring_channel_id'])

    def test_capability_research_does_not_promise_returning_or_unique_viewers(self):
        result = self.reports.capabilities()
        self.assertTrue(result['thumbnail_impressions_and_ctr']['available_on_this_page'])
        self.assertEqual(result['unique_viewers']['status'], 'not_supported')
        self.assertEqual(result['returning_viewers']['status'], 'not_documented')

    def test_model_validation_prevents_invalid_ids_ranges_and_duplicate_labels(self):
        for changes in [{'video_ids': []}, {'video_ids': ['bad']},
                        {'date_ranges': [{'label': 'x', 'start_date': '2025-01-02', 'end_date': '2025-01-01'}]},
                        {'date_ranges': [{'label': 'x', 'start_date': '2025-01-01', 'end_date': '2025-01-02'}]*2}]:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                suggested(**changes)


class ReportingEndpointTests(unittest.TestCase):
    def test_tools_annotations_and_authenticated_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(Path(tmp), 'https://youtube.example.com', '123', OWNED,
                                'test-client', 'test-secret', ['https://client.example.com/callback'])
            previous = os.umask(0o077)
            try:
                app = create_app(settings)
            finally:
                os.umask(previous)
            app.state.reports.suggested = Mock(return_value={'ranges': []})
            app.state.provider.load_access_token = AsyncMock(return_value=SimpleNamespace(scopes=['youtube:read']))
            body = suggested().model_dump(mode='json')
            with TestClient(app, base_url=settings.public_url) as client:
                self.assertEqual(client.post('/api/suggested-video-sources', json=body).status_code, 401)
                headers = {'Authorization': 'Bearer test-token'}
                self.assertEqual(client.post('/api/suggested-video-sources', headers=headers, json=body | {'video_ids': ['bad']}).status_code, 422)
                app.state.reports.suggested.assert_not_called()
                result = client.post('/api/suggested-video-sources?refresh=true', headers=headers, json=body)
                self.assertEqual(result.json(), {'ranges': []})
                self.assertTrue(app.state.reports.suggested.call_args.args[1])
            tools = {t.name: t for t in asyncio.run(app.state.mcp.list_tools())}
            self.assertFalse(tools['create_bulk_reporting_jobs'].annotations.readOnlyHint)
            self.assertTrue(tools['get_suggested_video_sources'].annotations.readOnlyHint)
            asyncio.run(app.state.mcp.call_tool('get_suggested_video_sources', {'query': body}))
            self.assertFalse(app.state.reports.suggested.call_args.args[1])
