"""Owned-video reports composed from supported, separately attributed queries."""
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

from .analytics_support import utc_now
from .bulk import BulkReports, REACH_REPORT, TRAFFIC_REPORT, report_date, timestamp
from .youtube import AnalyticsQuery, GoogleAPIError

SUMMARY_METRICS = 'views,engagedViews,estimatedMinutesWatched,averageViewDuration,likes,comments,shares,subscribersGained,subscribersLost'
DISCOVERY_METRICS = 'views,engagedViews,estimatedMinutesWatched'


def video_id(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', value):
        raise ValueError('Expected an 11-character YouTube video ID')
    return value


class OwnedVideos(BaseModel):
    video_ids: list[str] = Field(min_length=1, max_length=50)

    @field_validator('video_ids')
    @classmethod
    def valid_ids(cls, values):
        return list(dict.fromkeys(video_id(v) for v in values))


class DateRange(BaseModel):
    start_date: date
    end_date: date

    @model_validator(mode='after')
    def valid_dates(self):
        AnalyticsQuery(start_date=self.start_date, end_date=self.end_date, metrics='views')
        if (self.end_date - self.start_date).days > 3659:
            raise ValueError('A single report window is limited to 3660 reporting days')
        return self


class VideoPerformanceQuery(OwnedVideos, DateRange):
    pass


class NamedRange(DateRange):
    label: str = Field(min_length=1, max_length=100)


class SuggestedSourcesQuery(OwnedVideos):
    date_ranges: list[NamedRange] = Field(min_length=1, max_length=4)
    creator: str = Field(default='', max_length=100)
    source: Literal['analytics', 'bulk'] = 'analytics'
    max_metadata_videos: int = Field(default=500, ge=1, le=5000)

    @model_validator(mode='after')
    def unique_labels(self):
        if len({r.label for r in self.date_ranges}) != len(self.date_ranges):
            raise ValueError('Date-range labels must be unique')
        return self


class DiscoveryQuery(BaseModel):
    new_video_id: str
    older_video_ids: list[str] = Field(min_length=1, max_length=50)
    days_before: int = Field(default=28, ge=1, le=366, strict=True)
    days_after: int = Field(default=28, ge=1, le=366, strict=True)
    before: DateRange | None = None
    after: DateRange | None = None

    @field_validator('new_video_id')
    @classmethod
    def valid_new(cls, value):
        return video_id(value)

    @field_validator('older_video_ids')
    @classmethod
    def valid_old(cls, values):
        return list(dict.fromkeys(video_id(v) for v in values))


def number(value):
    if value in (None, '') or isinstance(value, bool):
        raise ValueError('Missing or invalid metric value in a report')
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError('Report metrics must be finite and nonnegative')
    return result


class ChannelReports:
    def __init__(self, youtube, bulk=None):
        self.yt = youtube
        self.bulk = bulk or BulkReports(youtube)

    def _metadata(self, ids, refresh=False):
        items, fetches = {}, []
        for offset in range(0, len(ids), 50):
            result = self.yt.request('data', 'videos', {'part': 'snippet,statistics,contentDetails',
                'id': ','.join(ids[offset:offset+50])}, refresh)
            fetches.append({'video_ids': ids[offset:offset+50],
                            'fetched_at': result['fetched_at'], 'cached': result['cached']})
            items.update({v['id']: v for v in result['data'].get('items', [])})
        return items, fetches

    def _owned(self, ids, refresh=False):
        self.yt.channel(refresh)
        items, fetches = self._metadata(ids, refresh)
        statuses = {v: ('unavailable' if v not in items else
                       'owned' if items[v].get('snippet', {}).get('channelId') == self.yt.settings.channel_id
                       else 'wrong_channel') for v in ids}
        return items, statuses, fetches

    def resolve_creator(self, creator, refresh=False):
        self.yt.channel()
        value = creator.strip()
        if re.fullmatch(r'UC[A-Za-z0-9_-]{22}', value):
            params = {'part': 'snippet', 'id': value}
        else:
            handle = value.removeprefix('@')
            if not re.fullmatch(r'[^\s/@?#:]{1,100}', handle):
                raise ValueError('creator must be a channel ID or handle, with or without one leading @')
            params = {'part': 'snippet', 'forHandle': handle}
        result = self.yt.request('data', 'channels', params, refresh)
        items = result['data'].get('items', [])
        if len(items) != 1:
            raise GoogleAPIError(404, 'Creator handle/channel could not be resolved; no name-based guess was applied.', source='data')
        channel = items[0]
        return {'channel_id': channel['id'], 'channel_title': channel['snippet']['title'],
                'handle': channel['snippet'].get('customUrl'), 'fetched_at': result['fetched_at'],
                'retrieved_at': utc_now(), 'cached': result['cached']}

    @staticmethod
    def _section(call):
        try:
            report = call()
            if isinstance(report, dict) and 'status' in report:
                return report
            return {'status': 'reported' if report.get('records') else 'no_data', 'report': report}
        except GoogleAPIError as error:
            # Authorization/quota failures remain visible with category and action.
            return {'status': 'error', 'error': error.as_dict()}
        except ValueError as error:
            return {'status': 'invalid_data', 'error': str(error)}

    def _daily(self, video, start, end, metrics, refresh):
        records, pages, index = [], [], 1
        expected = {(start + timedelta(days=i)).isoformat() for i in range((end-start).days+1)}
        for _ in range(20):
            report = self.yt.analytics(AnalyticsQuery(start_date=start, end_date=end,
                dimensions='day', metrics=metrics, filters='video==' + video, sort='day',
                start_index=index), refresh)
            records.extend(report['records'])
            pages.append({'fetched_at': report['fetched_at'], 'cached': report['cached'],
                          'observation_id': report.get('observation_id')})
            next_index = report['next_start_index']
            if next_index is None or expected <= {r['day'] for r in records}:
                break
            if next_index <= index:
                raise GoogleAPIError(502, 'Daily report pagination did not advance.', source='analytics')
            index = next_index
        else:
            raise GoogleAPIError(502, 'Daily report exceeded pagination budget; narrow the range.', source='analytics')
        present = {r['day'] for r in records}
        complete = (present == expected and len(records) == len(expected)
                    and all(all(r.get(m) is not None for m in metrics.split(',')) for r in records))
        return {'status': 'reported' if complete else 'incomplete_data', 'records': records,
                'missing_days': sorted(expected-present), 'pages': pages,
                'latest_observed_reporting_date': max(present, default=None),
                'note': 'Returned daily rows; missing days remain unknown. Even full reporting days may be revised.'}

    def performance(self, query: VideoPerformanceQuery, refresh=False):
        items, statuses, fetches = self._owned(query.video_ids, refresh)
        results = []
        for video in query.video_ids:
            item = {'video_id': video, 'status': statuses[video]}
            results.append(item)
            if statuses[video] != 'owned':
                continue
            item['metadata'] = items[video]
            def analytics(metrics, dimensions=''):
                return self.yt.analytics(AnalyticsQuery(start_date=query.start_date, end_date=query.end_date,
                    metrics=metrics, dimensions=dimensions, filters='video==' + video), refresh)
            item['summary'] = self._section(lambda: analytics(SUMMARY_METRICS))
            item['daily_coverage'] = self._section(lambda: self._daily(video, query.start_date, query.end_date, SUMMARY_METRICS, refresh))
            item['traffic_sources'] = self._section(lambda: analytics('views,estimatedMinutesWatched', 'insightTrafficSourceType'))
            item['devices'] = self._section(lambda: analytics('views,estimatedMinutesWatched', 'deviceType'))
            item['reach'] = self._section(lambda: self.reach([video], query.start_date, query.end_date))
            core = [item[k]['status'] for k in ('summary', 'daily_coverage', 'traffic_sources', 'devices')]
            item['status'] = 'reported' if all(s == 'reported' for s in core) else 'partial_data'
        return {'videos': results, 'start_date': query.start_date.isoformat(), 'end_date': query.end_date.isoformat(),
                'metadata_fetches': fetches, 'retrieved_at': utc_now(),
                'note': 'Separate compatible queries retain per-section provenance. Data API lifetime counts '
                        'are distinct from historical Analytics totals; absent reach imports are not zeros.'}

    def reach(self, video_ids, start, end):
        selection = self.bulk.select(REACH_REPORT, start, end)
        groups = {}
        for row in self.bulk.rows(selection, start, end, video_ids):
            key = (row['video_id'], report_date(row['date']).isoformat())
            if key in groups:
                raise ValueError('Reach-basic report has duplicate video/day rows; cannot safely aggregate')
            groups[key] = {'video_id': key[0], 'day': key[1],
                           'video_thumbnail_impressions': number(row['video_thumbnail_impressions']),
                           'video_thumbnail_impressions_ctr': number(row['video_thumbnail_impressions_ctr'])}
        # Keep original daily CTR; no naive average or inferred clicks.
        return {'status': 'reported' if groups and not selection['missing_report_days'] else
                         'incomplete_data' if groups else 'not_imported_or_no_rows',
                'daily': list(groups.values()), 'report_selection': selection, 'retrieved_at': utc_now(),
                'note': 'CTR is the reported daily percentage. No unweighted CTR average or estimated clicks is substituted.'}

    def suggested(self, query: SuggestedSourcesQuery, refresh=False):
        _, statuses, metadata_fetches = self._owned(query.video_ids, refresh)
        creator = self.resolve_creator(query.creator, refresh) if query.creator else None
        ranges, candidate_views, total_groups = [], defaultdict(float), 0
        for window in query.date_ranges:
            selection = self.bulk.select(TRAFFIC_REPORT, window.start_date, window.end_date) if query.source == 'bulk' else None
            uploads = []
            for video in query.video_ids:
                entry = {'video_id': video, 'status': statuses[video], 'sources': []}
                uploads.append(entry)
                if statuses[video] != 'owned':
                    continue
                try:
                    if query.source == 'analytics':
                        report = self.yt.analytics(AnalyticsQuery(start_date=window.start_date, end_date=window.end_date,
                            dimensions='insightTrafficSourceDetail', filters='video==' + video + ';insightTrafficSourceType==RELATED_VIDEO',
                            metrics='views,estimatedMinutesWatched', sort='-views', max_results=25), refresh)
                        source_rows = [{'referring_video_id': r['insightTrafficSourceDetail'], 'views': r['views'],
                                        'estimatedMinutesWatched': r['estimatedMinutesWatched']} for r in report['records']]
                        entry.update(status='reported' if source_rows else 'no_data',
                                     freshness=report.get('freshness'), fetched_at=report['fetched_at'],
                                     coverage=report.get('coverage', {'type': 'top_results', 'rank_limit': 25}),
                                     observation_id=report.get('observation_id'))
                    else:
                        source_rows = ({'referring_video_id': r['traffic_source_detail'] or None,
                                        'views': number(r['views']), 'estimatedMinutesWatched': number(r['watch_time_minutes'])}
                                       for r in self.bulk.rows(selection, window.start_date, window.end_date, [video])
                                       if r.get('traffic_source_type') == '7')
                        entry.update(report_selection=selection)
                    grouped = {}
                    for row in source_rows:
                        key = row['referring_video_id']
                        if key not in grouped:
                            total_groups += 1
                            if total_groups > 20_000:
                                raise ValueError('Referral result exceeds 20,000 groups; narrow the dates or upload selection')
                        source = grouped.setdefault(key, {'referring_video_id': key, 'views': 0, 'estimatedMinutesWatched': 0})
                        source['views'] += number(row['views'])
                        source['estimatedMinutesWatched'] += number(row['estimatedMinutesWatched'])
                        if key:
                            candidate_views[key] += number(row['views'])
                    entry['sources'] = list(grouped.values())
                    if query.source == 'bulk':
                        entry['status'] = ('incomplete_data' if selection['missing_report_days'] else 'reported') if grouped else 'not_imported_or_no_rows'
                except GoogleAPIError as error:
                    entry.update(status='error', error=error.as_dict())
                except ValueError as error:
                    entry.update(status='invalid_data', error=str(error))
            ranges.append({'label': window.label, 'start_date': window.start_date.isoformat(),
                           'end_date': window.end_date.isoformat(), 'uploads': uploads})
        candidates = sorted(candidate_views, key=lambda v: (-candidate_views[v], v))[:query.max_metadata_videos]
        referrers, ref_fetches, lookup_error = {}, [], None
        if candidates:
            try:
                referrers, ref_fetches = self._metadata(candidates, refresh)
            except GoogleAPIError as error:
                lookup_error = error.as_dict()
        unresolved = sorted(set(candidate_views) - set(referrers))
        for window in ranges:
            by_video, by_channel = {}, {}
            unidentified = False
            for upload in window['uploads']:
                enriched, unknown_sources = [], []
                for row in upload['sources']:
                    key = row['referring_video_id']
                    snippet = referrers.get(key, {}).get('snippet', {})
                    channel_id = snippet.get('channelId')
                    resolved = bool(snippet)
                    enriched_row = {**row, 'referring_video_title': snippet.get('title'),
                                    'referring_channel_id': channel_id, 'referring_channel_title': snippet.get('channelTitle'),
                                    'metadata_status': 'resolved' if resolved else 'unavailable_or_not_resolved',
                                    'average_duration_seconds': None}
                    unidentified |= not resolved
                    if creator and not resolved:
                        unknown_sources.append(enriched_row)
                    if creator and channel_id != creator['channel_id']:
                        continue
                    enriched.append(enriched_row)
                    aggregated = by_video.setdefault(key, {**enriched_row, 'views': 0, 'estimatedMinutesWatched': 0})
                    aggregated['views'] += row['views']
                    aggregated['estimatedMinutesWatched'] += row['estimatedMinutesWatched']
                    channel = by_channel.setdefault(channel_id, {'channel_id': channel_id,
                        'channel_title': snippet.get('channelTitle'), 'views': 0, 'estimatedMinutesWatched': 0,
                        'average_duration_seconds': None})
                    channel['views'] += row['views']
                    channel['estimatedMinutesWatched'] += row['estimatedMinutesWatched']
                upload['sources'] = enriched
                if creator:
                    upload['unresolved_sources_excluded_from_creator_filter'] = unknown_sources
            window.update(by_referring_video=sorted(by_video.values(), key=lambda r: -r['views']),
                          by_referring_channel=sorted(by_channel.values(), key=lambda r: -r['views']),
                          status='reported' if all(u['status'] == 'reported' for u in window['uploads']) else 'partial_data',
                          creator_filter_metadata_complete=not unidentified if creator else None)
        comparisons = []
        for current in ranges[1:]:
            original = {r['referring_video_id']: r for r in ranges[0]['by_referring_video']}
            latest = {r['referring_video_id']: r for r in current['by_referring_video']}
            comparisons.append({'from': ranges[0]['label'], 'to': current['label'], 'sources': [
                {'referring_video_id': key, 'visible_views_delta': latest[key]['views'] - original[key]['views']
                 if key in original and key in latest else None}
                for key in sorted(set(original) | set(latest), key=lambda k: k or '')]})
        return {'source': query.source, 'creator_filter': creator, 'ranges': ranges, 'comparisons': comparisons,
                'metadata_fetches': metadata_fetches + ref_fetches, 'retrieved_at': utc_now(),
                'metadata_resolution': {'candidate_count': len(candidate_views), 'requested_count': len(candidates),
                    'unresolved_video_ids': unresolved, 'error': lookup_error},
                'average_duration_note': 'No duration is derived from views and watch minutes: denominator compatibility is not guaranteed. '
                                         'Use separately reported averageViewDuration where supported.',
                'note': 'Analytics supplies top 25 per upload/window. Bulk uses imported latest revisions, subject to missing/anonymized rows. '
                        'Creator filtering applies to resolved stable channel IDs; unresolved identities are disclosed. '
                        'Ranges are compared separately, never summed across overlapping dates. Missing sources are unknown, not zero.'}

    def discovery(self, query: DiscoveryQuery, refresh=False):
        ids = list(dict.fromkeys([query.new_video_id] + query.older_video_ids))
        items, statuses, fetches = self._owned(ids, refresh)
        if statuses[query.new_video_id] != 'owned':
            return {'status': statuses[query.new_video_id], 'new_video_id': query.new_video_id, 'retrieved_at': utc_now()}
        publication = timestamp(items[query.new_video_id]['snippet']['publishedAt'])
        day = publication.astimezone(ZoneInfo('America/Los_Angeles')).date()
        before = (query.before.start_date, query.before.end_date) if query.before else (day-timedelta(days=query.days_before), day-timedelta(days=1))
        after = (query.after.start_date, query.after.end_date) if query.after else (day, day+timedelta(days=query.days_after-1))
        if before[1] >= day or after[0] < day or before[1] >= after[0]:
            raise ValueError('Before must end before the upload reporting day; after must start on/after it; windows must not overlap')
        results = []
        for video in query.older_video_ids:
            item = {'video_id': video, 'status': statuses[video]}
            results.append(item)
            if statuses[video] != 'owned':
                continue
            if timestamp(items[video]['snippet']['publishedAt']) >= publication:
                item['status'] = 'not_an_older_video'
                continue
            for name, (start, end) in [('before', before), ('after', after)]:
                if end >= datetime.now(ZoneInfo('America/Los_Angeles')).date():
                    report = {'status': 'window_not_finished', 'records': []}
                else:
                    report = self._section(lambda: self._daily(video, start, end, DISCOVERY_METRICS, refresh))
                report.update(start_date=start.isoformat(), end_date=end.isoformat(), days=(end-start).days+1)
                rows = report.get('records', [])
                if report['status'] == 'reported':
                    report['totals'] = {m: sum(number(r[m]) for r in rows) for m in DISCOVERY_METRICS.split(',')}
                    report['per_day'] = {m: v / report['days'] for m, v in report['totals'].items()}
                else:
                    report['totals'], report['per_day'] = None, None
                item[name] = report
            item['status'] = 'reported' if all(item[n]['status'] == 'reported' for n in ('before', 'after')) else 'incomplete_data'
            item['change'] = None
            if item['status'] == 'reported':
                item['change'] = {m: {'total_delta': item['after']['totals'][m] - item['before']['totals'][m],
                    'per_day_delta': item['after']['per_day'][m] - item['before']['per_day'][m],
                    'per_day_percent_change': ((item['after']['per_day'][m] / item['before']['per_day'][m] - 1) * 100)
                        if item['before']['per_day'][m] else None} for m in DISCOVERY_METRICS.split(',')}
        return {'new_video_id': query.new_video_id, 'publication_reporting_day': day.isoformat(),
                'reporting_timezone': 'America/Los_Angeles', 'upload_day_in_after': after[0] == day,
                'videos': results, 'metadata_fetches': fetches, 'retrieved_at': utc_now(),
                'note': 'Pre/post correlation only, not proof the new upload caused views or referred viewers. '
                        'Upload day is included in the default after window and can be partial. '
                        'Missing days keep totals/deltas unknown. Unequal windows include per-day changes; a zero baseline has no percent change.'}

    def capabilities(self, refresh=False):
        self.yt.channel(refresh)
        result = self.yt.request('reporting', 'reportTypes', {'pageSize': 100}, refresh)
        types = {r['id'] for r in result['data'].get('reportTypes', [])}
        return {'retrieved_at': utc_now(), 'fetched_at': result['fetched_at'],
                'available_report_types': result['data'].get('reportTypes', []),
                'next_page_token': result['data'].get('nextPageToken'),
                'thumbnail_impressions_and_ctr': {'documented': True, 'source': 'youtube_reporting_api',
                    'report_type': REACH_REPORT, 'available_on_this_page': REACH_REPORT in types,
                    'requires_job_and_import': True},
                'unique_viewers': {'status': 'not_supported',
                    'note': 'The historical uniques metric was deprecated in 2016; stale catalog mentions are not a supported query.'},
                'returning_viewers': {'status': 'not_documented',
                    'note': 'No supported returning-viewer metric was found in the reviewed Analytics/Reporting catalogs. Do not substitute subscribedStatus.'},
                'references': ['https://developers.google.com/youtube/reporting/v1/reports/channel_reports#reach-basic',
                               'https://developers.google.com/youtube/reporting/revision_history#september-27-2016']}
