import io
import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from contextlib import contextmanager

from inventory import (InventoryClient, _run_owned_inventory, default_config, load_config,
                       run_inventory, validate_config)
from monitor_schedule import SHANGHAI, default_schedule, schedule_state, validate_schedule
from protocol import Cancelled, Channel
from activity_log import ActivityLog
from inventory_broker import SharedClient


def at(value):
    return datetime.fromisoformat('2026-10-25T' + value).replace(tzinfo=SHANGHAI).timestamp()


def config(**schedule):
    return validate_config({'targets': [{'sku': 'MLH74FE', 'store_id': 'R705'}],
                            'schedule': dict(default_schedule(), enabled=True, **schedule)})


class ScheduleTests(unittest.TestCase):
    def test_boundaries_and_next_transition_use_shanghai(self):
        rule = config()['schedule']
        for value, paused, transition in [('00:59:59', False, '01:00:00'), ('01:00:00', True, '05:00:00'),
                                           ('04:59:59', True, '05:00:00'), ('05:00:00', False, None)]:
            state = schedule_state(rule, at(value))
            self.assertEqual(state['paused'], paused)
            self.assertEqual(state['reason'], 'excluded_window' if paused else None)
            if transition:
                self.assertEqual(state['next_transition_at_ms'], int(at(transition) * 1000))
        self.assertEqual(schedule_state(rule, datetime.fromisoformat('2026-10-24T17:00:00+00:00').timestamp())['paused'], True)
        self.assertEqual(schedule_state(default_schedule(), at('02:00:00'))['next_transition_at_ms'], None)

    def test_include_exclude_cross_midnight_and_clock_jumps(self):
        for mode in ('exclude', 'include'):
            rule = config(mode=mode, start='23:00', end='06:00')['schedule']
            for hour, inside in [('23:00:00', True), ('00:00:00', True), ('05:59:59', True),
                                 ('06:00:00', False), ('22:59:59', False), ('01:00:00', True)]:
                self.assertEqual(schedule_state(rule, at(hour))['paused'], inside if mode == 'exclude' else not inside)

    def test_strict_validation_and_legacy_omission_preserves_schedule(self):
        for field, values in {'enabled': [1, 'true', None], 'mode': ['other', None],
                              'start': ['1:00', '24:00', 100], 'end': ['05:60', '05:00:00'],
                              'timezone': ['UTC', None]}.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    validate_schedule(dict(default_schedule(), **{field: value}))
        with self.assertRaises(ValueError):
            config(start='01:00', end='01:00')
        self.assertFalse(validate_config({})['schedule']['enabled'])
        with tempfile.TemporaryDirectory() as directory:
            saved = config(mode='include', start='20:00', end='06:00')
            run_inventory('inventory_settings_save', {'config': saved}, None, directory)
            omitted = {key: value for key, value in saved.items() if key != 'schedule'}
            run_inventory('inventory_settings_save', {'config': omitted}, None, directory)
            self.assertEqual(load_config(directory)['schedule'], saved['schedule'])

    def test_save_reports_only_target_changes_for_queue_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            saved = config()

            def save(value):
                return run_inventory('inventory_settings_save', {'config': value}, None, directory)

            self.assertTrue(save(saved)['targets_changed'])
            for change in ({'schedule': dict(saved['schedule'], start='02:00')}, {'interval_seconds': 10},
                           {'proxy': dict(saved['proxy'], pool_size=20)}):
                saved = dict(saved, **change)
                self.assertFalse(save(saved)['targets_changed'])
            omitted = {key: value for key, value in saved.items() if key != 'schedule'}
            result = save(omitted)
            self.assertFalse(result['targets_changed'])
            self.assertEqual(result['config']['schedule'], saved['schedule'])
            changed = dict(saved, targets=[dict(saved['targets'][0], quantity=2)])
            self.assertTrue(save(changed)['targets_changed'])


class MonitorScheduleRuntimeTests(unittest.TestCase):
    def run_monitor(self, initial, clock, check=None, sleep=None, emit=None, load=None):
        channel = Channel('account', 'task', io.StringIO())
        created = []

        def client_factory(current, root):
            client = Mock(spec=InventoryClient)
            client.pool = None
            client.status.return_value = {'enabled': current['proxy']['enabled'], 'pool_size': 10}
            client.check.side_effect = lambda targets, **kwargs: check(channel, targets) if check else self.fail('paused monitor issued inventory request')
            created.append((current, client))
            return client

        original_emit = channel.emit

        def emitting(kind, **kwargs):
            original_emit(kind, **kwargs)
            if emit:
                emit(channel, kind, kwargs)

        channel.emit = emitting
        broker = Mock()
        broker.start.return_value = broker
        with tempfile.TemporaryDirectory() as directory, patch('inventory.InventoryClient', side_effect=client_factory), \
                patch('inventory.StoreBroker', return_value=broker), \
                patch('inventory.load_config', side_effect=lambda root: load() if load else initial), \
                patch('monitor_schedule.time.time', lambda: clock[0]), patch('inventory.time.sleep', lambda delay: sleep(channel, delay)), \
                self.assertRaises(Cancelled):
            _run_owned_inventory('inventory_monitor', {}, channel, Path(directory), initial, Mock())
        self.assertTrue(broker.close.called)
        self.assertTrue(all(client.close.called for _, client in created))
        return created, [json.loads(line) for line in channel.output.getvalue().splitlines()]

    def test_settings_read_crossing_wait_deadline_continues_monitor_without_negative_sleep(self):
        current = config()
        clock = [at('06:00:00')]
        monotonic = [0.0]
        waiting = [False]
        rounds = []
        sleeps = []

        def load():
            if waiting[0]:
                # A private-settings read can finish after the loop predicate's
                # deadline check, reproducing the real negative sleep failure.
                monotonic[0] += 1.1
                waiting[0] = False
            return current

        def check(channel, targets):
            rounds.append(monotonic[0])
            return {'targets': [dict(targets[0], status='unavailable')]}

        def emit(channel, kind, kwargs):
            if kind == 'inventory':
                if len(rounds) == 1:
                    waiting[0] = True
                else:
                    channel.cancelled.set()

        def sleep(channel, delay):
            if delay < 0:
                raise ValueError('sleep length must be non-negative')
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, .1)
            sleeps.append(delay)
            monotonic[0] += delay

        with patch('inventory.time.monotonic', lambda: monotonic[0]):
            _, events = self.run_monitor(current, clock, check=check, sleep=sleep, emit=emit, load=load)
        self.assertEqual(rounds, [0.0, 1.1])
        self.assertEqual(sleeps, [])
        self.assertEqual(sum(event['type'] == 'inventory' for event in events), 2)

    def test_paused_cancellation_no_query_no_proxy_construction(self):
        current = config()
        current['proxy']['enabled'] = True
        created, events = self.run_monitor(current, [at('02:00:00')], sleep=lambda channel, _: channel.cancelled.set())
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0][0]['proxy']['enabled'])
        self.assertTrue(current['proxy']['enabled'])
        result = next(event['result'] for event in events if event['type'] == 'inventory')
        self.assertEqual(result['targets'], [])
        self.assertIsNone(result['checked_at_ms'])
        self.assertIsNone(result['checked_at'])
        self.assertTrue(result['monitoring'])

    def test_clock_jump_resume_uses_fresh_query_and_restores_proxy(self):
        current = config()
        current['proxy']['enabled'] = True
        clock = [at('02:00:00')]
        queries = []

        def check(channel, targets):
            queries.append(clock[0])
            return {'targets': [dict(targets[0], status='available', checked_at_ms=int(clock[0] * 1000))]}

        def emit(channel, kind, kwargs):
            if kind == 'inventory' and kwargs['result']['targets']:
                channel.cancelled.set()

        created, events = self.run_monitor(current, clock, check=check,
                                          sleep=lambda channel, _: clock.__setitem__(0, at('05:00:00')), emit=emit)
        self.assertEqual([row[0]['proxy']['enabled'] for row in created], [False, True])
        self.assertEqual(queries, [at('05:00:00')])
        self.assertEqual(sum(event['type'] == 'inventory' for event in events), 2)

    def test_paused_settings_change_resumes_without_rewriting_targets(self):
        current = config()
        clock = [at('02:00:00')]
        original_targets = list(current['targets'])

        def sleep(channel, delay):
            self.assertLessEqual(delay, 1)
            current['schedule'] = default_schedule()

        def check(channel, targets):
            self.assertEqual(targets, original_targets)
            return {'targets': [dict(targets[0], status='unavailable')]}

        def emit(channel, kind, kwargs):
            if kind == 'inventory' and kwargs['result']['targets']:
                channel.cancelled.set()

        _, events = self.run_monitor(current, clock, check=check, sleep=sleep, emit=emit)
        self.assertFalse([event['result'] for event in events if event['type'] == 'inventory'][-1]['schedule_state']['paused'])

    def test_paused_summary_is_bounded_and_broker_remains_available(self):
        current = config()
        clock = [at('02:00:00')]
        elapsed = [0]

        def sleep(channel, delay):
            elapsed[0] += delay
            if elapsed[0] >= 31:
                channel.cancelled.set()

        with patch('inventory.time.monotonic', lambda: elapsed[0]):
            created, events = self.run_monitor(current, clock, sleep=sleep)
        self.assertEqual(sum(event['type'] == 'log' for event in events), 1)
        self.assertEqual(sum(event['type'] == 'inventory' for event in events), 2)
        self.assertFalse(created[0][1].warm.called)
        self.assertFalse(created[0][1].check.called)

    def test_inflight_round_crossing_pause_is_suppressed(self):
        current = config()
        clock = [at('00:59:59')]

        def check(channel, targets):
            clock[0] = at('01:00:00')
            return {'targets': [dict(targets[0], status='available', checked_at_ms=int(clock[0] * 1000))]}

        _, events = self.run_monitor(current, clock, check=check, sleep=lambda channel, _: channel.cancelled.set())
        results = [event['result'] for event in events if event['type'] == 'inventory']
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['schedule_state']['paused'])
        self.assertEqual(results[0]['targets'], [])

    def test_replace_crossing_pause_does_not_dispatch_new_round(self):
        current = config()
        current['proxy']['enabled'] = True
        clock = [at('02:00:00')]
        original_replace = SharedClient.replace
        switched = [False]

        def replace(shared, factory, channel):
            original_replace(shared, factory, channel)
            if not switched[0]:
                switched[0] = True
                clock[0] = at('01:00:00')

        def sleep(channel, delay):
            if switched[0]:
                channel.cancelled.set()
            else:
                clock[0] = at('00:59:59')

        with patch.object(SharedClient, 'replace', replace):
            created, events = self.run_monitor(current, clock, sleep=sleep)
        self.assertEqual([row[0]['proxy']['enabled'] for row in created], [False, True, False])
        self.assertTrue(all(not client.check.called for _, client in created))
        self.assertTrue(all(not event['result']['targets'] for event in events if event['type'] == 'inventory'))

    def test_borrow_crossing_pause_does_not_dispatch_new_round(self):
        current = config()
        clock = [at('00:59:59')]
        original_borrow = SharedClient.borrow

        @contextmanager
        def borrow(shared):
            with original_borrow(shared) as client:
                clock[0] = at('01:00:00')
                yield client

        with patch.object(SharedClient, 'borrow', borrow):
            created, events = self.run_monitor(current, clock, sleep=lambda channel, _: channel.cancelled.set())
        self.assertTrue(all(not client.check.called for _, client in created))
        result = next(event['result'] for event in events if event['type'] == 'inventory')
        self.assertTrue(result['schedule_state']['paused'])

    def test_crossing_round_does_not_persist_fresh_available_observation(self):
        current = config()
        clock = [at('00:59:59')]

        def fetch(client, url):
            clock[0] = at('01:00:00')
            return [{'storeNumber': 'R705', 'storeName': 'fixture', 'partsAvailability': {
                'MLH74FE/A': {'pickupDisplay': 'available'}}}], 'at', int(clock[0] * 1000)

        with tempfile.TemporaryDirectory() as directory, patch.object(InventoryClient, 'fetch', fetch):
            client = InventoryClient(current, directory)
            client.check(current['targets'], record_if=lambda: not schedule_state(current['schedule'], clock[0])['paused'])
            entries = ActivityLog(directory).query(limit=100)['entries']
            self.assertFalse(any(row['event'] in ('inventory_available', 'inventory_target_checked') for row in entries))


if __name__ == '__main__':
    unittest.main()
