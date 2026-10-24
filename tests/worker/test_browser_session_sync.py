"""Synthetic three-way cookie freshness; never opens an account or browser."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from browser_session_sync import sync_browser_session


def cookie(value='fixture-original', **fields):
    return dict(name='as_ltn_cn', value=value, domain='.apple.com.cn', path='/',
                secure=True, expires=None, **fields)


class Context:
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)
        self.added, self.removed = [], []
        self.fail = False

    def cookies(self):
        return copy.deepcopy(self.rows)

    def add_cookies(self, rows):
        if self.fail:
            raise OSError('fixture browser unavailable')
        for row in rows:
            self.rows = [item for item in self.rows if (item['name'],item['domain'],item['path']) !=
                         (row['name'],row['domain'],row['path'])]
            self.rows.append(copy.deepcopy(row))
            self.added.append(copy.deepcopy(row))

    def clear_cookies(self, **key):
        self.removed.append(key)
        self.rows = [row for row in self.rows if any(row[field] != value for field,value in key.items())]


class BrowserSessionSyncTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.service = SimpleNamespace(auth=self.root/'auth.json',account='sync@example.test')
        self.checkpoint = self.root/'.browser-session-sync.json'

    def source(self, rows, account='sync@example.test'):
        self.service.auth.write_text(json.dumps({'account':account,'cookies':{'account_session':rows}}),encoding='utf-8')

    def baseline(self):
        self.source([cookie()])
        context = Context([cookie()])
        sync_browser_session(context,self.service)
        return context

    def test_first_contact_preserves_existing_cookie_and_only_adds_missing(self):
        self.source([cookie('fixture-http')])
        context = Context([cookie('fixture-browser')])
        report = sync_browser_session(context,self.service)
        self.assertEqual(report['applied'],0)
        self.assertEqual(context.rows[0]['value'],'fixture-browser')
        self.assertNotIn('fixture-http',self.checkpoint.read_text())
        self.assertNotIn('fixture-browser',self.checkpoint.read_text())
        context = Context([])
        report = sync_browser_session(context,self.service)
        # A known browser branch changed after baseline; do not resurrect it.
        self.assertEqual(report['applied'],0)

    def test_first_missing_cookie_keeps_http_only_and_same_site(self):
        self.source([cookie('fixture-http',httpOnly=True,sameSite='Lax')])
        context = Context([])
        report = sync_browser_session(context,self.service)
        self.assertEqual(report['applied'],1)
        self.assertTrue(context.rows[0]['httpOnly'])
        self.assertEqual(context.rows[0]['sameSite'],'Lax')

    def test_new_http_key_replaces_only_unchanged_browser_value(self):
        context = self.baseline()
        context.rows[0]['httpOnly'] = True
        self.source([cookie('fixture-http-rotated')])
        report = sync_browser_session(context,self.service)
        self.assertEqual(report['applied'],1)
        self.assertEqual(context.rows[0]['value'],'fixture-http-rotated')
        self.assertTrue(context.rows[0]['httpOnly'])

    def test_independent_browser_rotation_never_consumes_pending_http_change(self):
        context = self.baseline()
        before = self.checkpoint.read_bytes()
        context.rows[0]['value'] = 'fixture-browser-rotated'
        self.source([cookie('fixture-http-rotated')])
        for _ in range(2):
            report = sync_browser_session(context,self.service)
            self.assertEqual(report['conflicts'],1)
            self.assertEqual(context.rows[0]['value'],'fixture-browser-rotated')
            self.assertEqual(self.checkpoint.read_bytes(),before)

    def test_browser_only_change_does_not_become_permission_to_overwrite_later(self):
        context = self.baseline()
        context.rows[0]['value'] = 'fixture-browser-rotated'
        sync_browser_session(context,self.service)
        self.source([cookie('fixture-later-http')])
        report = sync_browser_session(context,self.service)
        self.assertEqual(report['conflicts'],1)
        self.assertEqual(context.rows[0]['value'],'fixture-browser-rotated')

    def test_http_deletion_removes_only_exact_previously_managed_key(self):
        context = self.baseline()
        context.rows.extend([dict(cookie('fixture-path'),path='/shop'),
                             dict(cookie('fixture-shield'),name='shld_bt_ck'),
                             dict(cookie('fixture-trust'),name='DES123'),
                             dict(cookie('fixture-idms'),domain='idmsa.apple.com.cn')])
        self.source([])
        report = sync_browser_session(context,self.service)
        self.assertEqual(report['removed'],1)
        self.assertEqual(context.removed,[{'name':'as_ltn_cn','domain':'.apple.com.cn','path':'/'}])
        self.assertEqual(len(context.rows),4)

    def test_cdp_float_and_http_integer_expiry_do_not_create_false_conflicts(self):
        import time
        expiry = int(time.time()) + 600
        self.source([dict(cookie(),expires=expiry)])
        context = Context([dict(cookie(),expires=float(expiry))])
        sync_browser_session(context,self.service)
        self.source([dict(cookie('fixture-next'),expires=expiry)])
        self.assertEqual(sync_browser_session(context,self.service)['applied'],1)
        context.rows[0]['expires'] = float(expiry)
        self.source([dict(cookie('fixture-latest'),expires=expiry)])
        report = sync_browser_session(context,self.service)
        self.assertEqual(report['applied'],1)
        self.assertEqual(report['conflicts'],0)

    def test_host_only_and_domain_cookie_remain_distinct(self):
        self.source([cookie(),dict(cookie('fixture-host'),domain='apple.com.cn')])
        context = Context([cookie(),dict(cookie('fixture-host'),domain='apple.com.cn')])
        sync_browser_session(context,self.service)
        self.source([cookie(),dict(cookie('fixture-new-host'),domain='apple.com.cn')])
        sync_browser_session(context,self.service)
        self.assertEqual(len(context.added),1)
        self.assertEqual(context.added[0]['domain'],'apple.com.cn')
        self.assertEqual(next(row for row in context.rows if row['domain']=='.apple.com.cn')['value'],'fixture-original')

    def test_apply_failure_preserves_checkpoint_and_auth_then_can_retry(self):
        context = self.baseline()
        before = self.checkpoint.read_bytes()
        self.source([cookie('fixture-rotated')])
        auth = self.service.auth.read_bytes()
        context.fail = True
        self.assertTrue(sync_browser_session(context,self.service)['failed'])
        self.assertEqual(self.checkpoint.read_bytes(),before)
        self.assertEqual(self.service.auth.read_bytes(),auth)
        context.fail = False
        self.assertEqual(sync_browser_session(context,self.service)['applied'],1)

    def test_verified_browser_probe_rotation_converges_and_records_new_baseline(self):
        context = self.baseline()
        context.rows[0]['value'] = 'fixture-browser-login'
        exported = context.cookies()
        self.source([cookie('fixture-probe-rotated'),dict(cookie('fixture-new-probe'),name='myacinfo')])
        report = sync_browser_session(context,self.service,verified_export=exported)
        self.assertEqual(report['applied'],2)
        self.assertEqual(next(row for row in context.rows if row['name']=='as_ltn_cn')['value'],'fixture-probe-rotated')
        self.source([cookie('fixture-http-next'),dict(cookie('fixture-new-probe'),name='myacinfo')])
        self.assertEqual(sync_browser_session(context,self.service)['applied'],1)

    def test_verified_probe_never_overwrites_browser_changed_since_export(self):
        context = self.baseline()
        exported = context.cookies()
        context.rows[0]['value'] = 'fixture-browser-newer'
        self.source([cookie('fixture-probe')])
        report = sync_browser_session(context,self.service,verified_export=exported)
        self.assertEqual(report['conflicts'],1)
        self.assertEqual(context.rows[0]['value'],'fixture-browser-newer')

    def test_rejected_http_source_does_not_seed_or_consume_checkpoint_but_verified_can_converge(self):
        context = self.baseline()
        before = self.checkpoint.read_bytes()
        self.source([cookie('fixture-rejected-http')])
        self.service._http_session_rejected = True
        report = sync_browser_session(context,self.service)
        self.assertTrue(report['skipped_rejected'])
        self.assertEqual(self.checkpoint.read_bytes(),before)
        self.assertEqual(context.rows[0]['value'],'fixture-original')
        self.source([cookie('fixture-verified-probe')])
        self.assertEqual(sync_browser_session(context,self.service,verified_export=context.cookies())['applied'],1)

    def test_other_account_and_unapproved_cookie_families_are_never_imported(self):
        self.source([cookie()],account='other@example.test')
        context = Context([])
        sync_browser_session(context,self.service)
        self.assertFalse(self.checkpoint.exists())
        self.source([dict(cookie(),name='shld_bt_ck'),dict(cookie(),name='DES123'),
                     dict(cookie(),domain='idmsa.apple.com.cn')])
        self.assertEqual(sync_browser_session(context,self.service)['applied'],0)
        self.assertFalse(context.rows)


if __name__ == '__main__':
    unittest.main()
