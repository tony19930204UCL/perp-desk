"""Injected browser/GitHub adapters for the fail-closed relay cycle."""
import base64
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import relay_cycle

SNAPSHOT_JS = 'JSON.stringify({url:location.href,text:document.body.innerText,user_messages:[...document.querySelectorAll(\'[data-user-message-bubble] .whitespace-pre-wrap,[data-message-author-role="user"] .whitespace-pre-wrap\')].map(e=>e.innerText),composer_text:[...document.querySelectorAll(\'#prompt-textarea,[contenteditable="true"][role="textbox"]\')].filter(e=>e.getClientRects().length).map(e=>e.innerText).join(""),busy:[...document.querySelectorAll(\'button\')].some(e=>e.getClientRects().length&&(/^(停止|停止回應|Stop|Stop generating)$/i.test(e.getAttribute(\'aria-label\')||\'\')||e.getAttribute(\'data-testid\')===\'stop-button\')),buttons:[...document.querySelectorAll(\'button\')].map(e=>({text:e.innerText,aria:e.getAttribute(\'aria-label\')}))})'
FILL_JS = '(()=>{if(location.pathname!==PATHJSON)throw Error(\'Wrong chat\');const c=[...document.querySelectorAll(\'#prompt-textarea,[contenteditable="true"][role="textbox"]\')].filter(e=>e.getClientRects().length);if(c.length!==1)throw Error(\'Composer missing or ambiguous\');const e=c[0];if(e.innerText.trim())throw Error(\'Composer occupied\');e.focus();document.execCommand(\'insertText\',false,TEXTJSON);const norm=t=>t.replace(/\\r/g,\'\').replace(/\\n{2,}/g,\'\\n\').trim();if(norm(e.innerText)!==norm(TEXTJSON))throw Error(\'Composer mismatch\');return true})()'
SEND_JS = '(()=>{const b=[...document.querySelectorAll("button[data-testid=send-button],button[aria-label=傳送提示詞],button[aria-label=傳送訊息],button[aria-label=傳送]")].filter(e=>e.getClientRects().length);if(b.length!==1||b[0].disabled)throw Error("Send button unavailable");b[0].click();return true})()'
DETAILS_JS = '(()=>{const b=[...document.querySelectorAll("button")].filter(e=>e.innerText==="查看詳細資訊");if(b.length!==1)throw Error("Ambiguous details");b[0].click();return true})()'
DIALOG_JS = 'JSON.stringify([...document.querySelectorAll("[role=dialog]")].map(e=>e.innerText))'
CLOSE_JS = '(()=>{const b=[...document.querySelectorAll("button")].find(e=>e.innerText==="關閉對話框");if(b)b.click();return true})()'
ALLOW_JS = '(()=>{const b=[...document.querySelectorAll("button")].filter(e=>e.innerText.startsWith("允許一次"));if(b.length!==1)throw Error("Ambiguous approval");b[0].click();return true})()'
CARD = '要允許 ChatGPT 使用 GitHub 嗎？'
SECRET = re.compile(r'sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|PRIVATE KEY|api_key\\s*[:=]', re.I)


class TransportError(RuntimeError):
    pass


def _parsed(value):
    return json.loads(value) if isinstance(value, str) else value


def _retry(cdp, expression, sleep=time.sleep, tries=6, delay=2.0):
    for attempt in range(tries):
        try:
            value = cdp(expression)
            if value is not None and value != '' and value != [] and value != {}:
                return value
        except Exception:
            pass
        if attempt + 1 < tries:
            sleep(delay)
    raise TransportError('CDP unavailable or empty')


class BrowserTransport:
    def __init__(self, cdp, sleep=time.sleep, chat_home='https://chatgpt.com/'):
        self.cdp, self.sleep, self.chat_home = cdp, sleep, chat_home
        self._sent = None

    def _call(self, expr, tries=6, delay=2.0):
        return _retry(self.cdp, expr, self.sleep, tries, delay)

    def snapshot(self):
        data = _parsed(self._call(SNAPSHOT_JS))
        if not isinstance(data, dict):
            raise TransportError('invalid snapshot')
        return data

    def observe(self):
        s = self.snapshot()
        url = s.get('url', '')
        text = s.get('text', '')
        auth = bool(re.search(r'verify you are human', text, re.I) or
                    (re.search(r'Log in|Sign up|登入|註冊', text, re.I) and
                     re.search(r'password|welcome|密碼|歡迎', text, re.I)))
        return dict(route_ok=url == self.chat_home or bool(re.fullmatch(r'https://chatgpt\\.com/c/[^/?#]+', url)),
                    composer_empty=not s.get('composer_text', '').strip(),
                    busy=bool(s.get('busy')), auth_required=auth,
                    inline_approval_present=CARD in text)

    def send(self, prompt):
        if self.snapshot().get('url') != self.chat_home:
            self._call('(()=>{location.href=' + json.dumps(self.chat_home) + ';return true})()')
            stable = 0
            for _ in range(30):
                s = self.snapshot()
                stable = stable + 1 if (s.get('url') == self.chat_home and
                    not s.get('user_messages') and not s.get('busy')) else 0
                if stable == 3:
                    break
                self.sleep(2)
            else:
                raise TransportError('fresh chat not stable')
        o = self.observe()
        if not (o['route_ok'] and o['composer_empty']) or any(
                o[k] for k in ('busy', 'auth_required', 'inline_approval_present')):
            raise TransportError('pre-send refusal')
        self._call(FILL_JS.replace('PATHJSON', json.dumps('/')).replace('TEXTJSON', json.dumps(prompt)))
        self.cdp(SEND_JS)  # NEVER retry uncertain click
        self._sent = prompt

    @staticmethod
    def _norm(value):
        return re.sub(r'\\n{2,}', '\\n', value.replace('\\r', '').replace('`', '')).strip()

    def user_messages(self):
        bubbles = self.snapshot().get('user_messages', [])
        return [self._sent if self._sent is not None and self._norm(b) == self._norm(self._sent)
                else b for b in bubbles]

    def chat_url(self):
        return self.snapshot()['url']


def approve_github_card(cdp, repo, branch, allowed_paths, details_wait=1.0, sleep=time.sleep):
    if CARD not in _parsed(_retry(cdp, SNAPSHOT_JS, sleep)).get('text', ''):
        return {'found': False}
    try:
        _retry(cdp, DETAILS_JS, sleep)
        sleep(details_wait)
        dialogs = _parsed(_retry(cdp, DIALOG_JS, sleep))
        operations = []
        for dialog in dialogs:
            for match in re.finditer('call_tool\\n', dialog):
                operation, _ = json.JSONDecoder().raw_decode(dialog[match.end():].lstrip())
                operations.append(operation)
        if len(operations) != 1:
            raise ValueError('operation count')
        op = operations[0]
        tool = op['path'].rsplit('/', 1)[-1]
        args = op['args']
        content = args['content']
        if tool not in ('update_file', 'create_file'):
            raise ValueError('wrong tool')
        if args['repository_full_name'] != repo or args['branch'] != branch:
            raise ValueError('wrong repository or branch')
        if args['path'] not in allowed_paths:
            raise ValueError('wrong path')
        if not isinstance(content, str) or len(content) > 180000 or SECRET.search(content):
            raise ValueError('invalid or secret content')
    except (Exception) as exc:
        _retry(cdp, CLOSE_JS, sleep)
        return {'found': True, 'clicked': False, 'reason': str(exc)}
    _retry(cdp, CLOSE_JS, sleep)
    cdp(ALLOW_JS)  # non-idempotent; no retry
    return {'found': True, 'clicked': True, 'operation': tool,
            'file': args['path'], 'content_sha256': hashlib.sha256(content.encode()).hexdigest()}


class GitHubReceiptReader:
    def __init__(self, gh, repo, branch):
        self.gh, self.repo, self.branch = gh, repo, branch

    def read_receipt(self, unit):
        endpoint = 'repos/' + self.repo + '/contents/' + quote(unit['receipt_path'], safe='/') + '?ref=' + quote(self.branch, safe='')
        try:
            result = self.gh(endpoint)
        except Exception as exc:
            if '404' in str(exc) or 'Not Found' in str(exc):
                return None
            raise
        try:
            payload = json.loads(base64.b64decode(''.join(result['content'].split()), validate=True))
        except (ValueError, KeyError, TypeError):
            return None
        head = self.gh('repos/' + self.repo + '/git/ref/heads/' + self.branch)['object']['sha']
        return {'head_sha': head, 'blob_sha': result['sha'], 'payload': payload}


class DecisionPending(Exception):
    def __init__(self, unit_id, head_sha, blob_sha):
        self.unit_id, self.head_sha, self.blob_sha = unit_id, head_sha, blob_sha
        super().__init__(unit_id, head_sha, blob_sha)


def make_file_decider(decision_dir):
    def decide(unit, receipt):
        unit_id, head, blob = unit['unit_id'], receipt['head_sha'], receipt['blob_sha']
        for value in (unit_id, head, blob):
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
                raise ValueError('unsafe decision filename')
        path = Path(decision_dir) / (unit_id + '.' + head + '.' + blob + '.json')
        if not path.exists():
            raise DecisionPending(unit_id, head, blob)
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('status') not in ('ACCEPTED', 'REJECTED', 'BLOCKED') or not isinstance(data.get('reason'), str) or not data['reason'].strip() or not data.get('evidence') or data.get('head_sha') != head or data.get('blob_sha') != blob:
            raise ValueError('invalid operator decision')
        return {k: data[k] for k in ('status', 'reason', 'evidence')}
    return decide


def tick(config, cdp, gh, now=None):
    transport = BrowserTransport(cdp)
    reader = GitHubReceiptReader(gh, config['repo'], config['branch'])
    state = relay_cycle.describe_state(config['ledger_dir'])
    card = None
    if state.get('dispatch') and CARD in transport.snapshot().get('text', ''):
        card = approve_github_card(cdp, config['repo'], config['branch'], config['allowed_paths'])
    actual = dict(config, decide=make_file_decider(config['decision_dir']),
                  owner_id=config.get('owner_id', 'relay-adapter'))
    try:
        result = relay_cycle.run_cycle(actual, transport, reader, config['ledger_dir'],
                                       config['lock_dir'], now)
    except DecisionPending as exc:
        result = {'status': 'AWAITING_OPERATOR_DECISION', 'unit_id': exc.unit_id,
                  'head_sha': exc.head_sha, 'blob_sha': exc.blob_sha}
    if card is not None:
        result['card'] = card
    return result


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['tick'])
    parser.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    for key in ('repo', 'branch', 'ledger_dir', 'lock_dir', 'decision_dir',
                'resource_id', 'timeout_seconds', 'units', 'allowed_paths', 'bridge_command'):
        if key not in config:
            raise ValueError('missing config key: ' + key)
    if not isinstance(config['bridge_command'], list) or not config['bridge_command']:
        raise ValueError('invalid bridge_command')

    def cdp(expression):
        p = subprocess.run(config['bridge_command'], input=json.dumps(
            {'op': 'evaluate', 'expression': expression}), text=True, capture_output=True)
        if p.returncode or not p.stdout.strip():
            raise TransportError('bridge failed')
        return json.loads(p.stdout)

    def gh(endpoint):
        p = subprocess.run(['gh', 'api', endpoint], text=True, capture_output=True)
        if p.returncode or not p.stdout.strip():
            raise RuntimeError('gh api failed: ' + p.stderr)
        return json.loads(p.stdout)

    print(json.dumps(tick(config, cdp, gh), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
