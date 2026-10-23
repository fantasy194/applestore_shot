"""Offline IPC shim: run the actual main/Service/streaming queue, fake only Apple.

Used exclusively by the Rust purchase_queue cross-boundary test. Every socket,
HTTP request, browser launch and OS credential operation is forbidden. Actual
isolated child subprocesses use queue_bridge_child.py and synthetic payment data.
"""
from pathlib import Path
import json
import faulthandler
import os
import socket
import sys
import threading
import time

PROJECT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(PROJECT / 'worker'), str(PROJECT / 'worker' / 'legacy')]


def forbidden(*args, **kwargs):
    raise AssertionError('QUEUE_BRIDGE_FIXTURE_FORBIDS_NETWORK_BROWSER_AND_CREDENTIALS')


socket.create_connection = forbidden
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden

import requests
requests.sessions.Session.request = forbidden
import playwright.sync_api
playwright.sync_api.BrowserType.launch = forbidden
playwright.sync_api.BrowserType.launch_persistent_context = forbidden
playwright.sync_api.BrowserType.connect_over_cdp = forbidden

import credentials


class NoCredentials:
    get = save = delete = forbidden


credentials.Vault = NoCredentials
import duo_killer
duo_killer.purchase_sku = forbidden
duo_killer.login = forbidden
duo_killer.authenticate = forbidden

import protocol
import service
import queued_checkout
import main as actual_main

data = Path(sys.argv[sys.argv.index('--data-dir') + 1]).resolve()
audit = data / 'queue-bridge-audit'
audit.mkdir(parents=True, exist_ok=True)
audit_lock = threading.Lock()
stack_output = open(audit / 'thread-stacks.txt', 'w', encoding='utf-8')
faulthandler.dump_traceback_later(4, repeat=False, file=stack_output)


def record(direction, kind, payload):
    entry = {'direction': direction, 'type': kind, 'at_ns': time.monotonic_ns(), **payload}
    with open(audit / 'protocol.jsonl', 'a', encoding='utf-8') as output:
        output.write(json.dumps(entry, ensure_ascii=False) + '\n')


original_emit = protocol.Channel.emit
original_receive = protocol.Channel.receive


def audited_emit(channel, kind, **payload):
    with audit_lock:
        original_emit(channel, kind, **payload)
        record('out', kind, payload)


def audited_receive(channel, message):
    if message.get('type') in ('queue_add', 'queue_cancel', 'queue_close'):
        with audit_lock:
            record('in', message['type'], message)
    return original_receive(channel, message)


protocol.Channel.emit = audited_emit
protocol.Channel.receive = audited_receive


def audited_batch_method(name, operation):
    def run(owner, *args, **kwargs):
        with audit_lock:
            record('diagnostic', name + '_enter', {'thread': threading.current_thread().name})
        try:
            return operation(owner, *args, **kwargs)
        finally:
            with audit_lock:
                record('diagnostic', name + '_exit', {'thread': threading.current_thread().name})
    return run


if os.environ.get('DUO_QUEUE_BRIDGE_TRACE_BATCH'):
    for method in ('__init__', '_prepare', '_launch', 'run'):
        original = getattr(queued_checkout.PurchaseBatch, method)
        setattr(queued_checkout.PurchaseBatch, method, audited_batch_method('batch_' + method, original))


def checked_account(owner, *, enrolling=False):
    assert enrolling is False
    owner.channel.emit('stage', message='Offline fixture account already authenticated')
    return {'success': True, 'logged_in': True, 'scope': 'account', 'apple_id': owner.account}


service.Service.check_account = checked_account
actual_run_queued = queued_checkout.run_queued_purchase


def injected_children(owner, params, **options):
    assert params['queue_mode'] == 'parallel'
    assert params['queue_max_concurrency'] == 2
    assert options == {}
    return actual_run_queued(owner, params,
        worker_command=[sys.executable, str(Path(__file__).with_name('queue_bridge_child.py')),
                        '--fixture-audit', str(audit)], backoff_seconds=0)


queued_checkout.run_queued_purchase = injected_children

if __name__ == '__main__':
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='strict')
    actual_main.main()
