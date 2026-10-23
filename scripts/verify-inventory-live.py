"""Three anonymous polishing-cloth reads; never authenticate, add to bag or order."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'worker'))
from inventory import default_config, run_inventory, validate_config
from private_state import write_private
from protocol import Cancelled


class Probe:
    def __init__(self):
        self.cancelled = threading.Event()
        self.samples = []

    def checkpoint(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def emit(self, kind, **payload):
        if kind != 'inventory':
            return
        result = payload['result']
        self.samples.append({'received_at_ms': int(time.time() * 1000), 'checked_at': result['checked_at'],
            'targets': [{key: row.get(key) for key in ('sku', 'store_id', 'store_name', 'status',
                'checked_at_ms', 'pickup_message', 'error')} for row in result['targets']]})
        if len(self.samples) >= 3:
            self.cancelled.set()


def main():
    probe = Probe()
    config = default_config()
    config['targets'] = [{'id': 'readonly-cloth-' + store, 'sku': 'MLH74FE', 'quantity': 1,
        'store_id': store, 'store_name': name, 'location': '南京', 'account_id': '', 'auto_purchase': False}
        for store, name in [('R643', '虹悦城'), ('R688', '苏州')]]
    with tempfile.TemporaryDirectory(prefix='duo-inventory-readonly-') as folder:
        write_private(Path(folder) / 'inventory-settings.enc', validate_config(config))
        try:
            run_inventory('inventory_monitor', {}, probe, folder)
        except Cancelled:
            pass
    report = {'scope': 'anonymous_read_only_no_proxy_no_checkout', 'configured_interval_seconds': 1,
        'sample_count': len(probe.samples), 'samples': probe.samples}
    path = PROJECT / 'artifacts/inventory-live-monitor.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
