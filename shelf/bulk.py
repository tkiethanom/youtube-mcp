"""On-demand bulk reporting. No scheduler and no arbitrary download URLs."""
import csv
import hashlib
import io
import json
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from pydantic import BaseModel, Field, field_validator

from .analytics_support import utc_now
from .youtube import BASES, GoogleAPIError

TRAFFIC_REPORT = 'channel_traffic_source_a3'
REACH_REPORT = 'channel_reach_basic_a1'
REPORT_TYPES = (TRAFFIC_REPORT, REACH_REPORT)
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_CSV_ROWS = 1_000_000


class BulkSetupQuery(BaseModel):
    report_types: list[str] = Field(default_factory=lambda: list(REPORT_TYPES), min_length=1, max_length=2)

    @field_validator('report_types')
    @classmethod
    def valid_types(cls, values):
        if any(v not in REPORT_TYPES for v in values):
            raise ValueError('Supported job types: ' + ','.join(REPORT_TYPES))
        return list(dict.fromkeys(values))


def resource_id(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value):
        raise ValueError('Expected a Google reporting resource ID')
    return value


def report_date(value):
    return (datetime.strptime(value, '%Y%m%d').date() if re.fullmatch(r'\d{8}', value)
            else date.fromisoformat(value))


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Reporting timestamps must include a timezone')
    return parsed.astimezone(timezone.utc)


class BulkReports:
    def __init__(self, youtube):
        self.yt = youtube
        self.store = youtube.store

    def setup(self, query: BulkSetupQuery):
        # Serialize check/create against concurrent calls in the single-worker deployment.
        with self.yt.lock:
            self.yt.channel(True)
            available = self._all('reportTypes', 'reportTypes')
            allowed = {r['id'] for r in available}
            if any(t not in allowed for t in query.report_types):
                raise GoogleAPIError(404, 'Requested report type is not available to this account.', source='reporting')
            jobs = self._all('jobs', 'jobs')
            results = []
            for report_type in query.report_types:
                existing = next((j for j in jobs if j.get('reportTypeId') == report_type
                                 and (not j.get('expireTime') or timestamp(j['expireTime']) > timestamp(utc_now()))), None)
                if existing:
                    results.append({'status': 'existing', 'job': existing})
                    continue
                credentials = self.yt.credentials()
                try:
                    response = requests.post(BASES['reporting'] + 'jobs', json={
                        'reportTypeId': report_type, 'name': 'Intergalactic Shelf ' + report_type},
                        headers={'Authorization': 'Bearer ' + credentials.token},
                        timeout=(10, 45), allow_redirects=False)
                except requests.RequestException:
                    raise GoogleAPIError(502, 'Job creation outcome is unknown. List existing jobs before retrying.',
                                         category='transport') from None
                if response.status_code not in (200, 201):
                    raise GoogleAPIError.from_response(response, 'reporting')
                job = response.json()
                jobs.append(job)
                results.append({'status': 'created', 'job': job})
            return {'jobs': results, 'retrieved_at': utc_now(),
                    'note': 'YouTube generates these reports daily. Downloads/imports occur only when requested. '
                            'New jobs normally backfill 30 days; reports can take 24–48 hours to appear.'}

    def _all(self, path, key):
        items, token, seen = [], '', set()
        for _ in range(100):
            result = self.yt.request('reporting', path, {'pageSize': 100, **({'pageToken': token} if token else {})}, True)
            items.extend(result['data'].get(key, []))
            token = result['data'].get('nextPageToken', '')
            if not token:
                return items
            if token in seen:
                break
            seen.add(token)
        raise GoogleAPIError(502, 'Reporting pagination did not finish; no job was created. Try a narrower operation.', source='reporting')

    def sync(self, job_id, max_reports=10, page_token=''):
        resource_id(job_id)
        if not 1 <= max_reports <= 20:
            raise ValueError('max_reports must be 1–20; follow next_page_token for more')
        self.yt.channel()
        job = self.yt.request('reporting', 'jobs/' + job_id, {}, True)['data']
        if job.get('reportTypeId') not in REPORT_TYPES:
            raise ValueError('Only traffic-source and reach-basic imports are supported')
        page = self.yt.request('reporting', 'jobs/' + job_id + '/reports', {
            'pageSize': max_reports, **({'pageToken': page_token} if page_token else {})}, True)
        results = []
        for meta in page['data'].get('reports', []):
            try:
                results.append(self.import_report(job_id, meta['id'], job=job, metadata=meta))
            except (GoogleAPIError, ValueError) as error:
                results.append({'report_id': meta.get('id'), 'status': 'failed',
                                'error': error.as_dict() if isinstance(error, GoogleAPIError) else str(error)})
        return {'reports': results, 'next_page_token': page['data'].get('nextPageToken'),
                'retrieved_at': utc_now(), 'fetched_at': page['fetched_at'],
                'status': 'partial_failure' if any(r['status'] == 'failed' for r in results) else 'completed',
                'note': 'Imports are atomic per report. Retained revisions are selected by latest Google createTime, not summed.'}

    def import_report(self, job_id, report_id, *, job=None, metadata=None):
        resource_id(job_id)
        resource_id(report_id)
        if job is None or metadata is None:
            self.yt.channel()
            job = self.yt.request('reporting', 'jobs/' + job_id, {}, True)['data']
            metadata = self.yt.request('reporting', f'jobs/{job_id}/reports/{report_id}', {}, True)['data']
        report_type = job.get('reportTypeId')
        if report_type not in REPORT_TYPES:
            raise ValueError('Unsupported bulk report schema')
        if metadata.get('jobId', job_id) != job_id or metadata.get('id') != report_id:
            raise ValueError('Report metadata does not match the requested job/report')
        channel = self.yt.settings.channel_id
        with self.store.db() as db:
            existing = db.execute('SELECT imported_at FROM bulk_reports WHERE channel_id=? AND report_id=?',
                                  (channel, report_id)).fetchone()
        if existing:
            return {'report_id': report_id, 'status': 'already_imported', 'imported_at': existing[0]}
        start, end, created = (timestamp(metadata[k]) for k in ('startTime', 'endTime', 'createTime'))
        if end <= start or end - start > timedelta(days=2):
            raise ValueError('Expected a single daily report period')
        url = urlsplit(metadata.get('downloadUrl', ''))
        if (url.scheme != 'https' or url.hostname != 'youtubereporting.googleapis.com'
                or url.username or url.password or url.port not in (None, 443) or url.fragment
                or not url.path.startswith('/v1/media/')):
            raise ValueError('Google report download URL must be HTTPS on the Reporting API media endpoint')
        credentials = self.yt.credentials()
        try:
            response = requests.get(url.geturl(), headers={'Authorization': 'Bearer ' + credentials.token},
                                    stream=True, timeout=(10, 60), allow_redirects=False)
            try:
                if response.status_code != 200:
                    raise GoogleAPIError.from_response(response, 'reporting')
                with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode='w+b') as file:
                    size, digest = 0, hashlib.sha256()
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise ValueError('Bulk report exceeds the 64 MiB import limit; no data was imported')
                        digest.update(chunk)
                        file.write(chunk)
                    file.seek(0)
                    with io.TextIOWrapper(file, encoding='utf-8-sig', newline='') as text_file:
                        count = self._import_csv(text_file, channel, report_id, job_id, report_type,
                                                 metadata, digest.hexdigest(), start, end, created)
            finally:
                response.close()
        except requests.RequestException:
            raise GoogleAPIError(502, 'Bulk report download failed; no partial report was imported.', category='transport') from None
        except (csv.Error, UnicodeError):
            raise ValueError('Invalid CSV response; no report was imported') from None
        return {'report_id': report_id, 'report_type': report_type, 'status': 'imported',
                'rows': count, 'retrieved_at': utc_now()}

    def _import_csv(self, file, channel, report_id, job_id, report_type, metadata, digest, start, end, created):
        reader = csv.DictReader(file)
        required = {'date', 'channel_id', 'video_id'} | (
            {'views', 'watch_time_minutes', 'traffic_source_type', 'traffic_source_detail'}
            if report_type == TRAFFIC_REPORT else {'video_thumbnail_impressions', 'video_thumbnail_impressions_ctr'})
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)) or not required <= set(reader.fieldnames):
            raise ValueError('CSV is missing required columns or has duplicate headers; no report was imported')
        zone = ZoneInfo('America/Los_Angeles')
        first_day = start.astimezone(zone).date()
        last_day = (end - timedelta(microseconds=1)).astimezone(zone).date()
        if first_day != last_day:
            raise ValueError('Expected exactly one Pacific reporting day')
        count = 0
        with self.store.db() as db:
            # A competing import either completed already or will wait on this write transaction.
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT 1 FROM bulk_reports WHERE channel_id=? AND report_id=?', (channel, report_id)).fetchone()
            if existing:
                return 0
            for count, row in enumerate(reader, 1):
                if count > MAX_CSV_ROWS:
                    raise ValueError('Bulk report exceeds one million rows; no data was imported')
                if None in row or any(v is None for v in row.values()):
                    raise ValueError('CSV row length does not match headers')
                day = report_date(row['date'])
                if not first_day <= day <= last_day or row['channel_id'] not in ('', channel):
                    raise ValueError('CSV row is outside the report period or belongs to another channel')
                db.execute('INSERT INTO bulk_rows VALUES (?,?,?,?,?,?,?,?)',
                           (channel, report_id, count, day.isoformat(), row['video_id'],
                            row.get('traffic_source_type'), row.get('traffic_source_detail'), json.dumps(row)))
            safe_metadata = {k: v for k, v in metadata.items() if k != 'downloadUrl'}
            db.execute('INSERT INTO bulk_reports VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (channel, report_id, job_id, report_type, start.isoformat(), end.isoformat(),
                        created.isoformat(), utc_now(), digest, json.dumps(safe_metadata)))
        return count

    def select(self, report_type, start_date, end_date):
        """Select one latest revision per daily period; do not double-count backfills or duplicate jobs."""
        with self.store.db() as db:
            values = db.execute('''SELECT report_id,start_time,end_time,create_time,imported_at FROM bulk_reports
                WHERE channel_id=? AND report_type=? ORDER BY create_time, report_id''',
                (self.yt.settings.channel_id, report_type)).fetchall()
        latest = {}
        for row in values:
            first = timestamp(row[1]).astimezone(ZoneInfo('America/Los_Angeles')).date()
            last = (timestamp(row[2]) - timedelta(microseconds=1)).astimezone(ZoneInfo('America/Los_Angeles')).date()
            if first <= end_date and last >= start_date:
                latest[(row[1], row[2])] = row
        reports = list(latest.values())
        covered = set()
        for row in reports:
            day = timestamp(row[1]).astimezone(ZoneInfo('America/Los_Angeles')).date()
            last = (timestamp(row[2]) - timedelta(microseconds=1)).astimezone(ZoneInfo('America/Los_Angeles')).date()
            while day <= last:
                if start_date <= day <= end_date:
                    covered.add(day.isoformat())
                day += timedelta(days=1)
        expected = {(start_date + timedelta(days=i)).isoformat() for i in range((end_date - start_date).days + 1)}
        return {'report_ids': [r[0] for r in reports], 'report_type': report_type,
                'latest_imported_at': max((r[4] for r in reports), default=None),
                'latest_observed_reporting_date': max(covered, default=None),
                'missing_report_days': sorted(expected - covered),
                'report_days': sorted(covered),
                'coverage': 'available_reports' if expected <= covered else 'incomplete_reports',
                'note': 'On-demand imported bulk reports; latest revision per period only. '
                        'Reported rows may be anonymized or missing; imported days do not guarantee complete referral identities.'}

    def rows(self, selection, start_date, end_date, video_ids):
        # Keep CSV data bounded in memory even for large reports.
        with self.store.db() as db:
            for report_id in selection['report_ids']:
                for raw, in db.execute('''SELECT value_json FROM bulk_rows
                        WHERE channel_id=? AND report_id=? AND day>=? AND day<=? ORDER BY row_number''',
                        (self.yt.settings.channel_id, report_id, start_date.isoformat(), end_date.isoformat())):
                    row = json.loads(raw)
                    if row.get('video_id') in video_ids:
                        yield row
