import base64
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
import relay_adapters as a
import relay_cycle


class Page:
    def __init__(self):
        self.url = 'https://chatgpt.com/'
        self.text = ''
        self.composer = ''
        self.busy = False
        self.messages = []
        self.clicks = 0
        self.calls = []
        self.dialog = []
        self.fail_click = False
        self.snapshots = []

    def __call__(self, expr):
        self.calls.append(expr)
        if expr == a.SNAPSHOT_JS:
            if self.snapshots:
                return json.dumps(self.snapshots.pop(0))
            return json.dumps(dict(url=self.url, text=self.text, composer_text=self.composer,
                                   busy=self.busy, user_messages=self.messages))
        if expr == a.SEND_JS:
            self.clicks += 1
            if self.fail_click:
                raise RuntimeError('click failed')
            self.messages.append(self.composer.replace('`', ''))
            self.composer = ''
            self.url = 'https://chatgpt.com/c/abc'
            return True
        if expr.startswith('(()=>{location.href='):
            self.url = 'https://chatgpt.com/'
            self.messages = []
            return True
        if expr.startswith('(()=>{if(location.pathname'):
            match = re.search(r"insertText',false,(.*?)\);const norm", expr, re.DOTALL)
            self.composer = json.loads(match.group(1))
            return True
        if expr == a.DIALOG_JS:
            return json.dumps(self.dialog)
        return True


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.page = Page()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_js_constants_no_control_chars(self):
        CONTROLS = ('\r', '\n', '\t')
        self.assertTrue(any(ch in 'abc\nxyz' for ch in CONTROLS))
        self.assertFalse(any(ch in 'abc xyz rnt' for ch in CONTROLS))
        for name, value in vars(a).items():
            if not name.endswith('_JS') or not isinstance(value, str):
                continue
            with self.subTest(js=name):
                code = value.replace('PATHJSON', json.dumps('/')).replace(
                    'TEXTJSON', json.dumps('sample'))
                self.assertFalse(any(ch in code for ch in CONTROLS),
                                 name + ' contains a control character')

    def test_js_constants_parse_with_node_if_available(self):
        import shutil
        import subprocess
        node = shutil.which('node')
        if node is None:
            self.skipTest('node executable unavailable; mandatory control-character test still runs')
        for name, value in vars(a).items():
            if not name.endswith('_JS') or not isinstance(value, str):
                continue
            code = value.replace('PATHJSON', json.dumps('/')).replace(
                'TEXTJSON', json.dumps('sample'))
            with self.subTest(js=name):
                check = subprocess.run(
                    [node, '-e', 'new Function(' + json.dumps(code) + ')'],
                    capture_output=True, text=True)
                self.assertEqual(check.returncode, 0, check.stderr)

    def test_approval_refuses_hardcoded_github_token_value(self):
        result = self.approval(content='gh' + 'p_' + 'A' * 30)
        self.assertFalse(result['clicked'])
        self.assertEqual(self.page.calls.count(a.ALLOW_JS), 0)

    def test_approval_harmless_file_clicks_once(self):
        result = self.approval(content='print("safe")')
        self.assertTrue(result['clicked'])
        self.assertEqual(self.page.calls.count(a.ALLOW_JS), 1)

    def test_real_routes(self):
        transport = a.BrowserTransport(self.page, sleep=lambda _: None)
        for url in ('https://chatgpt.com/', 'https://chatgpt.com/c/abc123'):
            self.page.url = url
            self.assertTrue(transport.observe()['route_ok'], url)
        for url in ('https://chatgpt.com/settings', 'https://evil.com/c/abc',
                    'https://chatgpt.com/c/abc/extra'):
            self.page.url = url
            self.assertFalse(transport.observe()['route_ok'], url)

    def test_normalization_and_original_multiline(self):
        transport = a.BrowserTransport(self.page, sleep=lambda _: None)
        sample = 'a\n\n\nb\r' + '`'
        self.assertIn('\n', sample)
        self.assertEqual(transport._norm(sample), 'a\nb')
        transport._sent = 'a\nb `code`'
        self.page.messages = ['a\n\nb code']
        self.assertEqual(transport.user_messages(), [transport._sent])

    def test_approval_real_newline(self):
        self.assertTrue(self.approval()['clicked'])

    def test_retry_empty(self):
        values = iter(['', None, True])
        self.assertTrue(a.BrowserTransport(lambda _: next(values), sleep=lambda _: None)._call('x'))

    def test_retry_exception_cause(self):
        failure = RuntimeError('CDP disconnected')
        def broken(_):
            raise failure
        with self.assertRaises(a.TransportError) as caught:
            a.BrowserTransport(broken, sleep=lambda _: None)._call('x', tries=3)
        self.assertIs(caught.exception.__cause__, failure)
        self.assertIn('RuntimeError: CDP disconnected', str(caught.exception))

    def test_retry_exhausted(self):
        with self.assertRaises(a.TransportError):
            a.BrowserTransport(lambda _: None, sleep=lambda _: None)._call('x', tries=3)

    def test_occupied(self):
        self.page.composer = 'draft'
        with self.assertRaises(a.TransportError):
            a.BrowserTransport(self.page, sleep=lambda _: None).send('hello')
        self.assertEqual(self.page.clicks, 0)

    def test_busy(self):
        self.page.busy = True
        with self.assertRaises(a.TransportError):
            a.BrowserTransport(self.page, sleep=lambda _: None).send('hello')
        self.assertEqual(self.page.clicks, 0)

    def test_auth(self):
        self.page.text = 'Log in password'
        with self.assertRaises(a.TransportError):
            a.BrowserTransport(self.page, sleep=lambda _: None).send('hello')
        self.assertEqual(self.page.clicks, 0)

    def test_approval(self):
        self.page.text = a.CARD
        with self.assertRaises(a.TransportError):
            a.BrowserTransport(self.page, sleep=lambda _: None).send('hello')
        self.assertEqual(self.page.clicks, 0)

    def test_send_once(self):
        prompt = 'quotes " and \\ backslash\nnewline `backtick`'
        a.BrowserTransport(self.page, sleep=lambda _: None).send(prompt)
        self.assertEqual(self.page.messages[0], prompt.replace('`', ''))
        self.assertEqual(self.page.clicks, 1)

    def test_failed_click_not_retried(self):
        self.page.fail_click = True
        with self.assertRaises(RuntimeError):
            a.BrowserTransport(self.page, sleep=lambda _: None).send('hello')
        self.assertEqual(self.page.clicks, 1)

    def test_fresh_chat_three_stable(self):
        self.page.url = 'https://chatgpt.com/c/old'
        a.BrowserTransport(self.page, sleep=lambda _: None).send('new')
        self.assertEqual(self.page.clicks, 1)
        self.assertGreaterEqual(self.page.calls.count(a.SNAPSHOT_JS), 5)

    def test_original_prompt_and_raw(self):
        t = a.BrowserTransport(self.page, sleep=lambda _: None)
        t._sent = 'hello `world`'
        self.page.messages = ['hello world', 'other']
        self.assertEqual(t.user_messages(), ['hello `world`', 'other'])

    def approval(self, **changes):
        args = dict(repository_full_name='owner/repo', branch='dev', path='a.py', content='safe')
        args.update({k: v for k, v in changes.items() if k in args})
        op = dict(path='tools/update_file', args=args)
        op['path'] = changes.get('tool', op['path'])
        self.page.text = a.CARD
        self.page.dialog = ['call_tool\n' + json.dumps(op)]
        if changes.get('two'):
            self.page.dialog.append('call_tool\n' + json.dumps(op))
        return a.approve_github_card(self.page, 'owner/repo', 'dev', ['a.py'], sleep=lambda _: None)

    def test_approval_valid(self):
        self.assertTrue(self.approval()['clicked'])
        self.assertIn(a.ALLOW_JS, self.page.calls)

    def test_approval_wrong_repo(self):
        self.assertFalse(self.approval(repository_full_name='other/repo')['clicked'])

    def test_approval_wrong_branch(self):
        self.assertFalse(self.approval(branch='main')['clicked'])

    def test_approval_wrong_path(self):
        self.assertFalse(self.approval(path='other')['clicked'])

    def test_approval_wrong_tool(self):
        self.assertFalse(self.approval(tool='tools/delete_file')['clicked'])

    def test_approval_two_operations(self):
        self.assertFalse(self.approval(two=True)['clicked'])

    def test_approval_oversize(self):
        self.assertFalse(self.approval(content='x' * 180001)['clicked'])

    def test_approval_secret(self):
        self.assertFalse(self.approval(content='gh' + 'p_' + 'a' * 25)['clicked'])

    def test_approval_refusals_never_allow(self):
        self.approval(path='no')
        self.assertNotIn(a.ALLOW_JS, self.page.calls)

    def test_receipt_404(self):
        self.assertIsNone(a.GitHubReceiptReader(lambda _: (_ for _ in ()).throw(RuntimeError('404')), 'r/x', 'dev').read_receipt({'receipt_path': 'a'}))

    def test_receipt_invalid_json(self):
        reader = a.GitHubReceiptReader(lambda _: {'content': base64.b64encode(b'bad').decode(), 'sha': 'blob'}, 'r/x', 'dev')
        self.assertIsNone(reader.read_receipt({'receipt_path': 'a'}))

    def test_receipt_base64_newlines(self):
        def gh(endpoint):
            if '/git/ref/' in endpoint:
                return {'object': {'sha': 'head'}}
            encoded = base64.b64encode(b'{"ok":true}').decode()
            return {'content': encoded[:4] + '\n' + encoded[4:], 'sha': 'blob'}
        self.assertEqual(a.GitHubReceiptReader(gh, 'r/x', 'dev').read_receipt({'receipt_path': 'a'})['payload'], {'ok': True})

    def test_receipt_other_error(self):
        with self.assertRaises(RuntimeError):
            a.GitHubReceiptReader(lambda _: (_ for _ in ()).throw(RuntimeError('500')), 'r/x', 'dev').read_receipt({'receipt_path': 'a'})

    def decision(self, status='ACCEPTED', head='head'):
        path = self.root / 'a.head.blob.json'
        path.write_text(json.dumps(dict(status=status, reason='review', evidence='proof',
                                        head_sha=head, blob_sha='blob')))
        return a.make_file_decider(self.root)({'unit_id': 'a'}, {'head_sha': 'head', 'blob_sha': 'blob'})

    def test_decision_pending(self):
        with self.assertRaises(a.DecisionPending):
            a.make_file_decider(self.root)({'unit_id': 'a'}, {'head_sha': 'head', 'blob_sha': 'blob'})

    def test_decision_valid(self):
        self.assertEqual(self.decision()['status'], 'ACCEPTED')

    def test_decision_wrong_head(self):
        with self.assertRaises(ValueError):
            self.decision(head='wrong')

    def test_decision_bad_status(self):
        with self.assertRaises(ValueError):
            self.decision(status='PENDING')

    def test_tick_chain(self):
        units = [dict(unit_id=k, prompt='do ' + k, receipt_path=k + '.json',
                      depends_on=[] if k == 'a' else ['a'],
                      expected_receipt_validator_name='v', max_revisions=0)
                 for k in ('a', 'b')]
        config = dict(repo='r/x', branch='dev', ledger_dir=str(self.root / 'ledger'),
                      lock_dir=str(self.root / 'locks'), decision_dir=str(self.root),
                      resource_id='browser', timeout_seconds=120, units=units, allowed_paths=['a.py'])
        def gh(endpoint):
            unit = 'a' if '/contents/a.json' in endpoint else 'b'
            if '/git/ref/' in endpoint:
                return {'object': {'sha': 'head'}}
            record = relay_cycle.describe_state(config['ledger_dir'])['dispatch']
            payload = dict(schema_version=2, unit_id=unit, nonce=record['nonce'],
                           instruction_id=record['job_id'], validator_name='v',
                           timestamp=record['dispatched_at'] + 1)
            return {'sha': 'blob', 'content': base64.b64encode(json.dumps(payload).encode()).decode()}
        self.assertEqual(a.tick(config, self.page, gh)['status'], 'SENT')
        pending = a.tick(config, self.page, gh)
        self.assertEqual(pending['status'], 'AWAITING_OPERATOR_DECISION')
        self.decision()
        self.assertEqual(self.page.url, 'https://chatgpt.com/c/abc')
        self.assertEqual(a.tick(config, self.page, gh)['status'], 'SENT')
        self.assertTrue(any(call.startswith('(()=>{location.href=') for call in self.page.calls))
        self.assertEqual(a.tick(config, self.page, gh)['status'], 'AWAITING_OPERATOR_DECISION')
        (self.root / 'b.head.blob.json').write_text((self.root / 'a.head.blob.json').read_text())
        self.assertEqual(a.tick(config, self.page, gh)['status'], 'ALL_DONE')
        self.assertEqual(self.page.clicks, 2)

    def test_main_missing_config(self):
        path = self.root / 'config.json'
        path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'missing config'):
            a.main(['tick', '--config', str(path)])


if __name__ == '__main__':
    unittest.main()
