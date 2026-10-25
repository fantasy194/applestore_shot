"""Bounded, structured business activity shared by desktop and local Web workers.

The SQLite database contains public business diagnostics only. Credentials,
personal contact data, request bodies and payment handoff data never enter the
details schema. Free text is additionally redacted before it reaches storage.
"""
import argparse
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import time

RETENTION_MS = 7 * 24 * 60 * 60 * 1000
MAX_ROWS = 100_000
LEVELS = frozenset(('debug', 'info', 'warning', 'error'))
DETAIL_FIELDS = frozenset((
    'sku', 'skus', 'quantity', 'store_id', 'store_name', 'location', 'target_id', 'target_count',
    'status', 'reason', 'error_code', 'error_type', 'http_status', 'duration_ms', 'round', 'checked_at_ms',
    'proxy_enabled', 'pool_size', 'active_count', 'dispatchable_count', 'discarded_count', 'refill_count',
    'provider_id', 'requested_count', 'accepted_count', 'slot', 'expires_at', 'remaining_seconds',
    'available_count', 'unavailable_count', 'unknown_count', 'returned_store_count', 'interval_seconds',
    'concurrency', 'retry_count', 'attempts_made', 'retries_used', 'stop_reason', 'order_submitted', 'fulfillment',
    'success', 'prompt_kind', 'prepared', 'auto_purchase', 'operation', 'transport', 'browser_mode', 'configured',
    'queue_job_id', 'queue_assignment', 'sequence', 'enqueued_at_ms', 'mode', 'max_concurrency', 'queued_count', 'running_count', 'source',
    'browser_input_reason', 'browser_input_phase', 'browser_input_error',
))
IDENTIFIER = re.compile(r'[A-Za-z0-9_.:-]{1,100}')
INPUT_DIAGNOSTICS = {
    'browser_input_reason': {'document_changed', 'frame_detached', 'page_closed', 'deadline',
        'focus_changed', 'element_replaced', 'value_changed', 'field_missing', 'invalid_text', 'cdp_error'},
    'browser_input_phase': {'recognition', 'before_input', 'focus', 'clear', 'type', 'verify', 'submit', 'navigation', 'input'},
    'browser_input_error': {'timeout', 'detached_context', 'target_closed', 'protocol_error', 'other'},
}
SENSITIVE_ASSIGNMENT = re.compile(
    r'''(?ix)(["']?(?:password|passwd|pwd|token|access_token|refresh_token|id_token|authorization|cookie|set-cookie|otp|verification_code|验证码|密码|x-aos-stk|x-as-actk|csrf|secret|api_?key)["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}]+)''')


def safe_message(value):
    if not isinstance(value, str):
        return ''
    value = value.replace('\x00', '')[:8000]
    if re.search(r'(?i)请求详情|request\s*(?:body|headers)|response\s*body|截图|screenshot', value):
        return '私有请求或画面详情已省略。'
    if re.search(r'收货地址[：:]|送货地址[：:]|取货联系人[：:]|联系人[：:]|身份证[：:]', value):
        return '个人联系或地址信息已省略。'
    if SENSITIVE_ASSIGNMENT.search(value):
        return '私有认证字段已省略。'
    if re.search(r'''(?ix)\b(?:cookies?|password|passwd|pwd|token)\b\s*(?:[\{\[]|["']|\w+\s*=)''', value):
        return '私有认证字段已省略。'
    value = re.sub(r'(?i)data:(?:image|application)/[^\s]+', '[私有数据已隐藏]', value)
    value = re.sub(r'(?i)https?://[^\s<>"\']+', '[链接已隐藏]', value)
    value = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*', 'Bearer [已隐藏]', value)
    value = SENSITIVE_ASSIGNMENT.sub(r'\1[已隐藏]', value)
    value = re.sub(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', '[邮箱已隐藏]', value)
    value = re.sub(r'(?<!\d)1[3-9]\d{9}(?!\d)', '[手机已隐藏]', value)
    value = re.sub(r'(?i)((?:验证码|verification\s*code|one.time\s*code|OTP)\D{0,12})\d{4,8}', r'\1[已隐藏]', value)
    value = re.sub(r'(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{40,}(?![A-Za-z0-9])', '[长标识已隐藏]', value)
    return value[:2000]


def safe_details(details):
    if not isinstance(details, dict):
        return {}
    result = {}
    for key, value in details.items():
        if key not in DETAIL_FIELDS:
            continue
        if key in INPUT_DIAGNOSTICS:
            if isinstance(value, str) and value in INPUT_DIAGNOSTICS[key]:
                result[key] = value
            continue
        if value is None or type(value) is bool:
            result[key] = value
        elif type(value) in (int, float):
            if math.isfinite(value):
                result[key] = value
        elif isinstance(value, str):
            result[key] = safe_message(value)[:300]
        elif key == 'skus' and isinstance(value, list):
            result[key] = [safe_message(item)[:40] for item in value[:40] if isinstance(item, str)]
    return result


def category_for(action):
    if 'queue' in action:
        return 'queue'
    if action.startswith('inventory_') or action == 'store_search':
        return 'inventory'
    if action in ('purchase', 'checkout', 'add', 'clear_bag', 'bag', 'orders', 'cancel_order', 'qr'):
        return 'purchase'
    if action in ('login', 'enroll', 'me', 'import_auth', 'forget_password', 'session_maintain'):
        return 'authentication'
    return 'operation'


def _identifier(value, fallback=''):
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else fallback


def _enable_wal(connection):
    """Retry SQLite's first-time WAL transition while other writers initialize."""
    deadline = time.monotonic() + 5
    delay = 0.005
    while True:
        try:
            connection.execute('PRAGMA journal_mode=WAL')
            return
        except sqlite3.OperationalError as error:
            if not any(word in str(error).casefold() for word in ('locked', 'busy')):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.1)


class ActivityLog:
    def __init__(self, data_dir, *, account_id='', task_id='', action='', category=None,
                 now=None, retention_ms=RETENTION_MS, max_rows=MAX_ROWS):
        self.path = Path(data_dir) / 'logs' / 'activity.sqlite'
        self.account_id = _identifier(account_id)
        self.task_id = _identifier(task_id)
        self.action = _identifier(action, 'operation')
        self.category = _identifier(category, category_for(self.action))
        self.now = now or (lambda: int(time.time() * 1000))
        self.retention_ms = retention_ms
        self.max_rows = max_rows
        self.last_error = None
        self.started = time.perf_counter()
        self._stock_states = {}

    @classmethod
    def for_settings(cls, settings_dir, **context):
        root = Path(settings_dir)
        return cls(root.parent if root.name == 'settings' else root, **context)

    def _connect(self, *, write):
        connection = None
        try:
            if write:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                connection = sqlite3.connect(self.path, timeout=5)
            else:
                connection = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
            connection.execute('PRAGMA busy_timeout=5000')
            if write:
                _enable_wal(connection)
                connection.execute('PRAGMA synchronous=NORMAL')
                connection.execute('PRAGMA wal_autocheckpoint=256')
                connection.execute('PRAGMA journal_size_limit=8388608')
                connection.execute('''CREATE TABLE IF NOT EXISTS activity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
                    account_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    category TEXT NOT NULL, action TEXT NOT NULL, level TEXT NOT NULL,
                    event TEXT NOT NULL, message TEXT NOT NULL, details TEXT NOT NULL)''')
                connection.execute('CREATE INDEX IF NOT EXISTS activity_ts ON activity(ts)')
                connection.execute('CREATE INDEX IF NOT EXISTS activity_account_id ON activity(account_id,id)')
                connection.execute('CREATE INDEX IF NOT EXISTS activity_task_id ON activity(task_id,id)')
                connection.execute('CREATE INDEX IF NOT EXISTS activity_category_id ON activity(category,id)')
            connection.row_factory = sqlite3.Row
            return connection
        except BaseException:
            if connection:
                connection.close()
            raise

    def record(self, event, message, *, details=None, level='info', category=None):
        ids = self.record_many([dict(event=event, message=message, details=details, level=level, category=category)])
        return ids[0] if ids else None

    def record_stock_observations(self, observations, *, now_ms):
        """Persist confirmed stock episodes atomically with their public history.

        Every confirmation advances a durable timestamp high-water mark. The
        common unchanged case uses a conditional UPDATE without reading state.
        Unknown/stale observations never erase an available episode.
        """
        candidates = {}
        for item in observations:
            if not isinstance(item, dict):
                continue
            sku, store, sampled = item.get('sku'), item.get('store_id'), item.get('checked_at_ms')
            status = item.get('status')
            if (not isinstance(sku, str) or not re.fullmatch(r'[A-Z0-9]{5,30}(?:/A)?', sku)
                    or not isinstance(store, str) or not re.fullmatch(r'R\d{3,5}', store)
                    or type(sampled) is not int or not 0 < sampled <= now_ms
                    or now_ms - sampled > 10_000 or status not in ('available', 'unavailable')):
                continue
            key = (sku.removesuffix('/A'), store)
            previous = candidates.get(key)
            if previous is None or sampled > previous['checked_at_ms']:
                candidates[key] = {'sku': key[0], 'store_id': store,
                                   'store_name': safe_message(item.get('store_name', ''))[:100],
                                   'status': status, 'checked_at_ms': sampled}
        changed = {}
        for key, item in candidates.items():
            cached = self._stock_states.get(key)
            if cached and item['checked_at_ms'] <= cached['checked_at_ms']:
                continue
            changed[key] = item
        if not changed:
            return []
        connection = None
        try:
            connection = self._connect(write=True)
            ids = []
            confirmed = {}
            with connection:
                connection.execute('CREATE TABLE IF NOT EXISTS stock_episode ('
                                   'sku TEXT NOT NULL, store_id TEXT NOT NULL, status TEXT NOT NULL,'
                                   'checked_at_ms INTEGER NOT NULL, PRIMARY KEY(sku,store_id))')
                # Serialize competing clients' read/transition/write operations.
                connection.execute('BEGIN IMMEDIATE')
                for key, item in changed.items():
                    advanced = connection.execute('UPDATE stock_episode SET checked_at_ms=? '
                        'WHERE sku=? AND store_id=? AND status=? AND checked_at_ms < ?',
                        (item['checked_at_ms'], *key, item['status'], item['checked_at_ms'])).rowcount
                    if advanced:
                        confirmed[key] = item
                        continue
                    previous = connection.execute('SELECT status,checked_at_ms FROM stock_episode '
                                                  'WHERE sku=? AND store_id=?', key).fetchone()
                    if previous and item['checked_at_ms'] <= previous['checked_at_ms']:
                        confirmed[key] = dict(item, status=previous['status'], checked_at_ms=previous['checked_at_ms'])
                        continue
                    if item['status'] == 'available' and (not previous or previous['status'] != 'available'):
                        ids.append(connection.execute('INSERT INTO activity('
                            'ts,account_id,task_id,category,action,level,event,message,details) VALUES (?,?,?,?,?,?,?,?,?)',
                            (int(self.now()), '', self.task_id, 'stock', 'inventory_check', 'info',
                             'inventory_available', '门店商品有货。', json.dumps(item, ensure_ascii=False))).lastrowid)
                    if not previous or previous['status'] != item['status']:
                        connection.execute('INSERT INTO stock_episode(sku,store_id,status,checked_at_ms) VALUES (?,?,?,?) '
                                           'ON CONFLICT(sku,store_id) DO UPDATE SET status=excluded.status,'
                                           'checked_at_ms=excluded.checked_at_ms', (*key, item['status'], item['checked_at_ms']))
                    confirmed[key] = item
            self._stock_states.update(confirmed)
            self.last_error = None
            return ids
        except (sqlite3.Error, OSError, ValueError):
            self.last_error = 'activity_storage_unavailable'
            return []
        finally:
            if connection:
                connection.close()

    def record_many(self, entries):
        rows = []
        timestamp = int(self.now())
        for entry in entries[:500]:
            rows.append((timestamp, _identifier(entry.get('account_id'), self.account_id), self.task_id,
                         _identifier(entry.get('category'), self.category), self.action,
                         entry.get('level') if entry.get('level') in LEVELS else 'info',
                         _identifier(entry.get('event'), 'activity'), safe_message(entry.get('message', '')),
                         json.dumps(safe_details(entry.get('details')), ensure_ascii=False)))
        if not rows:
            return []
        connection = None
        try:
            connection = self._connect(write=True)
            with connection:
                ids = [connection.execute('INSERT INTO activity(ts,account_id,task_id,category,action,level,event,message,details) '
                                          'VALUES (?,?,?,?,?,?,?,?,?)', row).lastrowid for row in rows]
                connection.execute('DELETE FROM activity WHERE ts < ?', (timestamp - self.retention_ms,))
                connection.execute('DELETE FROM activity WHERE id <= COALESCE('
                                   '(SELECT id FROM activity ORDER BY id DESC LIMIT 1 OFFSET ?),0)', (self.max_rows,))
            self.last_error = None
            return ids
        except (sqlite3.Error, OSError, ValueError):
            # Diagnostics must never interrupt a checkout or trigger a replay.
            self.last_error = 'activity_storage_unavailable'
            return []
        finally:
            if connection:
                connection.close()

    def observe(self, kind, payload):
        queue_context = {key: payload[key] for key in ('queue_job_id', 'queue_assignment') if key in payload}
        if kind in ('stage', 'log'):
            self.record('task_' + kind, payload.get('message', ''),
                        details={**queue_context, **{key: payload[key] for key in INPUT_DIAGNOSTICS if key in payload},
                                 'order_submitted': payload.get('order_submitted') is True},
                        category='queue' if queue_context else None)
        elif kind == 'prompt':
            prompt = payload.get('prompt') if isinstance(payload.get('prompt'), dict) else {}
            self.record('task_prompt', '任务等待人工操作。', details={'prompt_kind': prompt.get('kind')})
        elif kind == 'result':
            result = payload.get('result') if isinstance(payload.get('result'), dict) else {}
            self.record('operation_end', '操作完成。' if result.get('success') is not False else '操作返回失败。',
                        details={'success': result.get('success') is not False, 'status': result.get('status'),
                                 'duration_ms': int((time.perf_counter() - self.started) * 1000)},
                        level='error' if result.get('success') is False else 'info')
        elif kind in ('error', 'cancelled'):
            self.record('operation_' + kind, payload.get('message', '操作已停止。'),
                        details={'order_submitted': payload.get('order_submitted') is True,
                                 'duration_ms': int((time.perf_counter() - self.started) * 1000)},
                        level='error' if kind == 'error' else 'warning')
        elif kind in ('progress_result', 'queue_progress'):
            result = payload.get('result') if isinstance(payload.get('result'), dict) else {}
            retry = safe_details(result.get('retry_summary'))
            retry_context = {key: retry[key] for key in ('retry_count', 'attempts_made', 'retries_used', 'stop_reason') if key in retry}
            self.record('queue_progress' if kind == 'queue_progress' else 'purchase_progress', '下单状态已更新。', details={
                **queue_context, **retry_context, 'status': result.get('status'),
                'order_submitted': payload.get('order_submitted') is True or result.get('status') == 'pending_payment'},
                category='queue' if kind == 'queue_progress' or queue_context else None)

    def query(self, *, limit=100, before_id=None, after_id=None, account_id=None, task_id=None,
              category=None, action=None, level=None, since_ms=None, until_ms=None):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('日志每页数量必须为 1–500。')
        conditions, values = ['ts >= ?'], [int(self.now()) - self.retention_ms]
        if before_id is not None and after_id is not None:
            raise ValueError('日志前后游标不能同时使用。')
        if after_id is not None:
            if type(after_id) is not int or not 0 <= after_id <= 9_223_372_036_854_775_807:
                raise ValueError('日志分页游标无效。')
            conditions.append('id > ?')
            values.append(after_id)
        if before_id is not None:
            if type(before_id) is not int or not 1 <= before_id <= 9_223_372_036_854_775_807:
                raise ValueError('日志分页游标无效。')
            conditions.append('id < ?')
            values.append(before_id)
        for name, value in (('account_id', account_id), ('task_id', task_id), ('category', category), ('action', action), ('level', level)):
            if value is not None and value != '':
                if not _identifier(value) or name == 'level' and value not in LEVELS:
                    raise ValueError('日志筛选条件无效。')
                conditions.append(name + ' = ?')
                values.append(value)
        for comparison, value in (('>=', since_ms), ('<=', until_ms)):
            if value is not None:
                if type(value) is not int or not 0 <= value <= 253_402_300_799_999:
                    raise ValueError('日志时间范围无效。')
                conditions.append('ts ' + comparison + ' ?')
                values.append(value)
        if not self.path.exists():
            return {'entries': [], 'next_before_id': None, 'has_more': False,
                    **({'next_after_id': after_id} if after_id is not None else {})}
        connection = None
        try:
            connection = self._connect(write=False)
            direction = 'ASC' if after_id is not None else 'DESC'
            sql = 'SELECT * FROM activity' + (' WHERE ' + ' AND '.join(conditions) if conditions else '') + f' ORDER BY id {direction} LIMIT ?'
            rows = connection.execute(sql, [*values, limit + 1]).fetchall()
            has_more = len(rows) > limit
            entries = []
            for row in rows[:limit]:
                entry = dict(row)
                entry['details'] = safe_details(json.loads(entry['details']))
                entry['message'] = safe_message(entry['message'])
                entries.append(entry)
            return {'entries': entries, 'next_before_id': entries[-1]['id'] if after_id is None and has_more and entries else None,
                    'has_more': has_more,
                    **({'next_after_id': entries[-1]['id'] if entries else after_id} if after_id is not None else {})}
        except (sqlite3.Error, OSError, ValueError):
            raise ValueError('操作日志暂时无法读取，请稍后重试。') from None
        finally:
            if connection:
                connection.close()


def serve(data_dir, source=None, output=None):
    source = source or sys.stdin
    output = output or sys.stdout
    while True:
        line = source.readline(1_048_577)
        if not line:
            return
        too_large = len(line) > 1_048_576
        try:
            if too_large:
                raise ValueError()
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get('params', {}), dict):
                raise ValueError()
            params = request.get('params', {})
            if request.get('operation') == 'append':
                activity = ActivityLog(data_dir, **{key: params[key] for key in ('account_id', 'task_id', 'action', 'category') if key in params})
                identifier = activity.record(params.get('event', 'activity'), params.get('message', ''),
                                             details=params.get('details'), level=params.get('level', 'info'))
                if identifier is None:
                    raise ValueError()
                result = {'id': identifier}
            elif request.get('operation') == 'query':
                result = ActivityLog(data_dir).query(**params)
            else:
                raise ValueError()
            reply = {'ok': True, 'result': result}
        except (ValueError, TypeError, sqlite3.Error, OSError):
            reply = {'ok': False, 'error': '操作日志请求未完成，请检查参数或稍后重试。'}
        output.write(json.dumps(reply, ensure_ascii=False) + '\n')
        output.flush()
        if too_large:
            return


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--activity-log', choices=('query', 'serve'), required=True)
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--limit', type=int, default=100)
    parser.add_argument('--before-id', type=int)
    parser.add_argument('--after-id', type=int)
    for name in ('account-id', 'task-id', 'category', 'action', 'level'):
        parser.add_argument('--' + name)
    parser.add_argument('--since-ms', type=int)
    parser.add_argument('--until-ms', type=int)
    args = vars(parser.parse_args(argv))
    mode = args.pop('activity_log')
    data = args.pop('data_dir')
    if mode == 'serve':
        serve(data)
        return
    try:
        print(json.dumps(ActivityLog(data).query(**args), ensure_ascii=False))
    except ValueError as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
