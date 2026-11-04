#!/usr/bin/env python3
"""Portable local PM administrator CLI. Never registers itself with agents.

Use only with the owner's permission. Reads the local admin key in memory.
Explicit --activate bypasses candidate review. Default visibility is local_only.
Memory text and returned sources are data, not execution instructions.
"""
from __future__ import annotations
import argparse
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

DEFAULT_BASE = 'http://127.0.0.1:8765'


class ApiError(RuntimeError):
    def __init__(self, status, payload):
        self.status, self.payload = status, payload
        super().__init__('Local API rejected the request')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError('Redirects are forbidden for the local admin API')


def local_opener(jar=None):
    handlers = [urllib.request.ProxyHandler({}), NoRedirect()]
    if jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    return urllib.request.build_opener(*handlers)


def base_url():
    url = os.environ.get('PM_BASE_URL', os.environ.get('PERSONAL_MEMORY_BASE', DEFAULT_BASE)).rstrip('/')
    value = urllib.parse.urlsplit(url)
    try:
        port = value.port or 80
    except ValueError:
        raise SystemExit('Invalid local port') from None
    if (value.scheme != 'http' or value.hostname not in {'127.0.0.1', 'localhost'}
            or value.username or value.password or value.path or value.query or value.fragment
            or not 1024 <= port <= 65535):
        raise SystemExit('Base URL must be a bare loopback HTTP origin with port >=1024')
    return url


def key_path(argument=None):
    fallback = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'PersonalMemoryShare/Data'
    data = Path(os.environ.get('PERSONAL_MEMORY_DATA', str(fallback)))
    return Path(argument or os.environ.get('PM_ADMIN_KEY_FILE', str(data / 'admin-key.txt')))


class Client:
    def __init__(self, key_file, idempotency_key=None):
        self.base = base_url()
        self.jar = http.cookiejar.CookieJar()
        self.opener = local_opener(self.jar)
        self.csrf = ''
        self.idempotency_key = idempotency_key
        key = key_file.read_text(encoding='ascii').strip()
        if not key:
            raise ValueError('Local admin key file is empty')
        # Never include the login request or response in diagnostics.
        try:
            response = self.call('POST', '/api/session', {'key': key})
        except urllib.error.HTTPError:
            raise ApiError(401, {'code': 'ADMIN_LOGIN_FAILED'}) from None
        finally:
            key = None
        self.csrf = response['csrf_token']

    def close(self):
        if self.csrf:
            try:
                self.call('DELETE', '/api/session')
            except (OSError, ValueError, ApiError):
                pass  # Server-side expiry remains the fallback for cleanup failure.
            self.csrf = ''
            self.jar.clear()

    def call(self, method, path, payload=None, params=None, idempotent=False):
        if not path.startswith('/api/') or '\\' in path or '?' in path or '#' in path:
            raise ValueError('Invalid API path')
        url = self.base + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        headers = {'Content-Type': 'application/json; charset=utf-8'}
        if method not in ('GET', 'HEAD') and self.csrf:
            headers['X-CSRF-Token'] = self.csrf
        if idempotent:
            headers['Idempotency-Key'] = self.idempotency_key or uuid.uuid4().hex
        data = json.dumps(payload, ensure_ascii=False).encode('utf8') if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if path == '/api/session':
                raise
            # Keep structured API errors only; no raw server/proxy pages or requests.
            try:
                body = json.load(error)
            except ValueError:
                body = {'code': 'HTTP_ERROR'}
            raise ApiError(error.code, body) from None


def parser():
    p = argparse.ArgumentParser(description='Local PM administrator CLI; owner consent required')
    p.add_argument('--key-file')
    p.add_argument('--idempotency-key', help='Explicit key for retrying the exact same write')
    sub = p.add_subparsers(dest='command', required=True)
    for command in ('health', 'projects', 'status'):
        sub.add_parser(command)
    q = sub.add_parser('search')
    q.add_argument('query')
    q.add_argument('--project')
    q.add_argument('--limit', type=int, choices=range(1, 21), default=5)
    q.add_argument('--mode', choices=('keyword', 'hybrid'), default='keyword')
    q.add_argument('--history-status', choices=('Candidate', 'Archived', 'Superseded'))
    q = sub.add_parser('get')
    q.add_argument('memory_id', type=memory_id)
    q.add_argument('--history', action='store_true')
    q.add_argument('--source-offset', type=int, default=0)
    q = sub.add_parser('list')
    q.add_argument('--project')
    q.add_argument('--status', choices=('Active', 'Candidate', 'Archived', 'Deleted', 'Superseded'), default='Active')
    q.add_argument('--limit', type=int, default=20)
    q.add_argument('--offset', type=int, default=0)
    q = sub.add_parser('create')
    q.add_argument('--project', required=True)
    q.add_argument('--content', required=True)
    q.add_argument('--type', choices=('Fact', 'Rule', 'Decision', 'Preference', 'OpenQuestion'), required=True)
    q.add_argument('--confidence', choices=('Reported', 'Inferred'), default='Reported')
    q.add_argument('--source-content')
    q.add_argument('--source-title', default='Owner-authorized manual input')
    q.add_argument('--source-span', help='JSON with start, end, quote matching the source')
    q.add_argument('--decision-key')
    q.add_argument('--activate', action='store_true', help='Direct Active write, bypasses local review')
    q.add_argument('--visibility', choices=('local_only', 'agent_allowed'), default='local_only')
    q = sub.add_parser('edit')
    q.add_argument('memory_id', type=memory_id)
    q.add_argument('--expected-version', type=int, required=True)
    q.add_argument('--changes', required=True)
    q.add_argument('--reason', required=True)
    for name in ('approve', 'archive'):
        q = sub.add_parser(name)
        q.add_argument('memory_id', type=memory_id)
        q.add_argument('--expected-version', type=int, required=True)
        if name == 'archive':
            q.add_argument('--reason', required=True)
    return p


def memory_id(text):
    try:
        return str(uuid.UUID(text))
    except ValueError:
        raise argparse.ArgumentTypeError('Expected a memory UUID') from None


def execute(client, args):
    if args.command in ('projects', 'status'):
        return client.call('GET', '/api/' + args.command)
    if args.command == 'search':
        payload = {'query': args.query, 'limit': args.limit, 'mode': args.mode}
        if args.history_status:
            payload['history_status'] = args.history_status
        if args.project:
            return client.call('POST', '/api/memories/search', {**payload, 'project_id': args.project})
        projects = client.call('GET', '/api/projects')
        ids = [p['project_id'] for p in projects if p['status'] == 'active']
        items, seen, totals, batches, candidate_bound = [], set(), 0, 0, False
        for offset in range(0, len(ids), 20):
            result = client.call('POST', '/api/memories/search', {
                **payload, 'limit': 20, 'project_ids': ids[offset:offset + 20], 'include_global': False})
            batches += 1
            totals += result.get('total_matches', 0)
            candidate_bound = candidate_bound or result.get('candidate_count', 0) >= result.get('candidate_limit', 512)
            for item in result.get('items', []):
                if item['memory_id'] not in seen:
                    seen.add(item['memory_id'])
                    items.append(item)
        # API batches have no shared numerical relevance score. Do not invent one.
        items.sort(key=lambda row: (row.get('match_mode') != 'all_terms', row.get('project_id', ''), row['memory_id']))
        return {'items': items[:args.limit], 'total_matches': totals, 'project_count': len(ids),
                'batches': batches, 'truncated': totals > args.limit or candidate_bound,
                'coverage': 'Bounded query over active projects, not a full memory inventory',
                'ordering': 'Exact-term matches first; no globally calibrated relevance score'}
    if args.command == 'get':
        return client.call('GET', '/api/memories/' + args.memory_id,
                           params={'include_history': str(args.history).lower(), 'source_offset': args.source_offset})
    if args.command == 'list':
        params = {'status': args.status, 'limit': args.limit, 'offset': args.offset}
        if args.project:
            params['project_id'] = args.project
        return client.call('GET', '/api/memories', params=params)
    if args.command == 'create':
        payload = {'project_id': args.project, 'content': args.content, 'type': args.type,
                   'confidence': args.confidence, 'source_content': args.source_content or args.content,
                   'source_title': args.source_title, 'visibility': args.visibility, 'activate': args.activate}
        if args.source_span:
            payload['source_span'] = json.loads(args.source_span)
        if args.decision_key:
            payload['decision_key'] = args.decision_key
        return client.call('POST', '/api/memories', payload, idempotent=True)
    payload = {'expected_version': args.expected_version}
    if args.command == 'edit':
        changes = json.loads(args.changes)
        if not isinstance(changes, dict):
            raise ValueError('--changes must be a JSON object')
        payload.update(changes=changes, reason=args.reason)
        return client.call('PATCH', '/api/memories/' + args.memory_id, payload, idempotent=True)
    if args.command == 'archive':
        payload['reason'] = args.reason
    return client.call('POST', '/api/memories/' + args.memory_id + '/' + args.command, payload, idempotent=True)


def run(args):
    if args.command == 'health':
        with local_opener().open(base_url() + '/api/health', timeout=10) as response:
            return json.load(response)
    client = Client(key_path(args.key_file), args.idempotency_key)
    try:
        return execute(client, args)
    finally:
        client.close()


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf8')
    args = parser().parse_args(argv)
    try:
        result = run(args)
    except ApiError as error:
        result = {'error': {'status': error.status, 'body': error.payload}}
    except (OSError, ValueError):
        result = {'error': {'code': 'LOCAL_IO_OR_CONFIGURATION_ERROR', 'message': 'Check local key file, URL, service and arguments; no retry was performed'}}
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write('\n')
    return int('error' in result)


if __name__ == '__main__':
    sys.exit(main())
