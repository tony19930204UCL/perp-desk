#!/usr/bin/env python3
"""Opt-in, stdlib-only REAL Chromium/CDP acceptance for the HTTP-served D2 dashboard.

Not part of unittest discovery. Does not navigate an existing logged-in browser.
Run with --expected-head and --output; unavailable Chromium is NOT_RUN, never PASS.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

import dashboard_v2_server as server
from dashboard_v2_reader import read_dashboard

LAB = Path(__file__).resolve().parent
REPO = LAB.parent
FIXTURE = LAB / 'tests' / 'fixtures' / 'dashboard_v2_browser.json'
PROBES = (
    'D2-BROWSER-POPULATED-1280-390',
    'D2-BROWSER-HOSTILE-TEXT',
    'D2-BROWSER-UNKNOWN-VS-ZERO',
    'D2-BROWSER-STALE-FETCH-CLEARING',
    'D2-BROWSER-NO-EXTERNAL-REQUESTS',
)
TRACKED = {
    'fixture': FIXTURE,
    'server': LAB / 'dashboard_v2_server.py',
    'reader': LAB / 'dashboard_v2_reader.py',
    'model': LAB / 'dashboard_v2_model.py',
    'html': LAB / 'dashboard_v2.html',
    'js': LAB / 'dashboard_v2_ui.js',
}
INJECTIONS = ('status', 'invalid_json', 'invalid_model', 'network_fail', 'hold')


class AcceptanceTimeout(TimeoutError):
    pass


class Deadline:
    def __init__(self, seconds):
        self.end = time.monotonic() + seconds

    def left(self, cap=10):
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise AcceptanceTimeout('acceptance deadline reached')
        return min(cap, remaining)


def fixture_contract(data):
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise ValueError('fixture schema_version must equal 1')
    if not isinstance(data.get('now_ms'), int) or isinstance(data['now_ms'], bool):
        raise ValueError('fixture clock must be an integer')
    if not isinstance(data.get('scorecard'), dict) or not isinstance(data['scorecard'].get('arms'), list):
        raise ValueError('fixture scorecard is invalid')
    if not isinstance(data.get('journal'), list) or not isinstance(data.get('ledger'), dict):
        raise ValueError('fixture sources are invalid')
    if not isinstance(data['ledger'].get('items'), list):
        raise ValueError('fixture ledger items are invalid')
    if not isinstance(data.get('hostile'), str) or not isinstance(data.get('long_text'), str):
        raise ValueError('fixture text inputs are invalid')
    if len(data['long_text']) < 120:
        raise ValueError('fixture long text is not long')
    return data


def write_sources(root, fixture):
    """Write only temporary synthetic inputs using the existing reader shapes."""
    paths = [root / name for name in ('scorecard.jsonl', 'journal.jsonl', 'ledger.json')]
    paths[0].write_text(json.dumps(fixture['scorecard'], ensure_ascii=False) + '\n', encoding='utf-8')
    paths[1].write_text(''.join(json.dumps(entry, ensure_ascii=False) + '\n'
                                for entry in fixture['journal']), encoding='utf-8')
    paths[2].write_text(json.dumps(fixture['ledger'], ensure_ascii=False), encoding='utf-8')
    return paths


def state(paths):
    return {name: {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                   'size': path.stat().st_size, 'mtime_ns': path.stat().st_mtime_ns}
            for name, path in paths.items()}


def git_head():
    result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=REPO, timeout=3,
                            capture_output=True, text=True, check=True)
    value = result.stdout.strip()
    if not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ValueError('checkout HEAD is not a full SHA')
    return value


def browser_binary(override):
    if override:
        path = Path(override).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            return None
        return str(path.resolve())
    for name in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'):
        candidate = shutil.which(name)
        if candidate:
            return candidate
    return None


def permitted_origin(url, origin):
    """Only exact loopback origin and four fixed server paths are expected."""
    a, b = urlsplit(url), urlsplit(origin)
    return (a.scheme == 'http' and a.hostname == b.hostname == '127.0.0.1'
            and a.port == b.port and a.username is None and a.password is None
            and a.path in ('/', '/dashboard_v2.html', '/dashboard_v2_ui.js', '/api/dashboard')
            and not a.query and not a.fragment)


def chrome_frame(sock, payload):
    raw = payload.encode('utf-8')
    mask = os.urandom(4)
    size = len(raw)
    header = bytearray([0x81])
    if size < 126:
        header.append(0x80 | size)
    elif size < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack('>H', size))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack('>Q', size))
    sock.sendall(bytes(header) + mask +
                 bytes(byte ^ mask[index % 4] for index, byte in enumerate(raw)))


class ChromeCDP:
    """Single private Chromium profile; WebSocket CDP including Fetch interception."""

    def __init__(self, binary, profile, origin, deadline):
        self.deadline = deadline
        self.origin = origin
        self.next_id = 0
        self.trace = []
        self.violations = []
        self.inject = []
        self.held = {}
        self.failed_loads = []
        self.events_seen = set()
        self.sock = None
        self.process = None
        flags = [binary, '--headless=new', '--no-first-run', '--no-default-browser-check',
                 '--disable-background-networking', '--disable-extensions',
                 '--disable-default-apps', '--disable-dev-shm-usage', '--disable-gpu',
                 '--no-proxy-server', '--no-sandbox', '--remote-debugging-address=127.0.0.1',
                 '--remote-debugging-port=0', '--user-data-dir=' + str(profile), 'about:blank']
        self.process = subprocess.Popen(flags, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        active = profile / 'DevToolsActivePort'
        target = None
        while self.deadline.left(1):
            if self.process.poll() is not None:
                raise RuntimeError('private Chromium process exited during startup')
            try:
                port_text = active.read_text(encoding='ascii').splitlines()[0]
                port = int(port_text)
                with urlopen('http://127.0.0.1:%d/json' % port, timeout=self.deadline.left(1)) as response:
                    targets = json.loads(response.read(131072))
                target = next((item for item in targets if item.get('type') == 'page'), None)
                if target:
                    break
            except (OSError, UnicodeError, ValueError, IndexError):
                pass
            time.sleep(min(.05, self.deadline.left(.1)))
        self.sock = self._connect(target['webSocketDebuggerUrl'])
        self.call('Page.enable')
        self.call('Runtime.enable')
        self.call('Network.enable')
        # Every request is paused before loading; external URLs are failed locally.
        self.call('Fetch.enable', {'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}]})

    def _connect(self, url):
        info = urlsplit(url)
        if info.scheme != 'ws' or info.hostname != '127.0.0.1':
            raise RuntimeError('CDP endpoint is not loopback')
        sock = socket.create_connection(('127.0.0.1', info.port), timeout=self.deadline.left(3))
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        sock.sendall(('GET ' + info.path + ' HTTP/1.1\r\nHost: 127.0.0.1:' +
                      str(info.port) + '\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n' +
                      'Sec-WebSocket-Key: ' + key + '\r\nSec-WebSocket-Version: 13\r\n\r\n').encode('ascii'))
        response = b''
        while b'\r\n\r\n' not in response:
            response += sock.recv(4096)
            if len(response) > 16384:
                raise RuntimeError('CDP handshake too large')
        if b' 101 ' not in response.split(b'\r\n', 1)[0]:
            raise RuntimeError('CDP handshake refused')
        return sock

    def _exact(self, size):
        chunks = []
        while size:
            part = self.sock.recv(size)
            if not part:
                raise RuntimeError('private Chromium CDP socket closed')
            chunks.append(part)
            size -= len(part)
        return b''.join(chunks)

    def _receive(self, timeout):
        self.sock.settimeout(self.deadline.left(timeout))
        first, second = self._exact(2)
        opcode, size = first & 15, second & 127
        if size == 126:
            size = struct.unpack('>H', self._exact(2))[0]
        elif size == 127:
            size = struct.unpack('>Q', self._exact(8))[0]
        if size > 8000000:
            raise RuntimeError('oversized CDP frame')
        mask = self._exact(4) if second & 128 else None
        raw = self._exact(size)
        if mask:
            raw = bytes(b ^ mask[i % 4] for i, b in enumerate(raw))
        if opcode == 8:
            raise RuntimeError('Chromium closed CDP session')
        if opcode == 9:
            chrome_frame(self.sock, json.dumps({'method': 'Runtime.evaluate'}))
            return None
        if opcode != 1:
            return None
        return json.loads(raw)

    def _send(self, method, params=None):
        self.next_id += 1
        chrome_frame(self.sock, json.dumps({'id': self.next_id, 'method': method,
                                            'params': params or {}}, separators=(',', ':')))
        return self.next_id

    def call(self, method, params=None):
        ident = self._send(method, params)
        while True:
            event = self._receive(self.deadline.left(8))
            if event is None:
                continue
            if event.get('id') == ident:
                if 'error' in event:
                    raise RuntimeError(method + ' CDP error: ' + str(event['error'])[:200])
                return event.get('result', {})
            if 'method' in event:
                self._event(event)

    def pump(self, seconds=.15):
        try:
            event = self._receive(seconds)
        except socket.timeout:
            return
        if event and 'method' in event:
            self._event(event)

    def _event(self, event):
        method = event['method']
        p = event.get('params', {})
        if method in ('Network.requestWillBeSent', 'Network.responseReceived',
                      'Network.loadingFailed', 'Network.loadingFinished', 'Fetch.requestPaused'):
            url = (p.get('request') or p.get('response') or {}).get('url')
            item = {'event': method, 'request_id': p.get('requestId'),
                    'url': url, 'method': (p.get('request') or {}).get('method'),
                    'error': p.get('errorText'),
                    'status': (p.get('response') or {}).get('status')}
            self.trace.append(item)
            if url and not permitted_origin(url, self.origin):
                self.violations.append(item)
            if method == 'Network.loadingFailed':
                self.failed_loads.append(item)
        if method != 'Fetch.requestPaused':
            return
        request_id = p['requestId']
        url = p['request']['url']
        if not permitted_origin(url, self.origin):
            self._send('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'BlockedByClient'})
            return
        if urlsplit(url).path != '/api/dashboard' or not self.inject:
            self._send('Fetch.continueRequest', {'requestId': request_id})
            return
        mode = self.inject.pop(0)
        if mode == 'hold':
            self.held['older'] = request_id
            return
        if mode == 'network_fail':
            self._send('Fetch.failRequest', {'requestId': request_id, 'errorReason': 'Failed'})
            return
        status = 503 if mode == 'status' else 200
        raw = (b'{"error":"synthetic HTTP failure"}' if mode == 'status' else
               b'{' if mode == 'invalid_json' else b'{"arms":[]}')
        self._send('Fetch.fulfillRequest', {
            'requestId': request_id, 'responseCode': status,
            'responseHeaders': [{'name': 'Content-Type', 'value': 'application/json; charset=utf-8'},
                                {'name': 'Cache-Control', 'value': 'no-store'}],
            'body': base64.b64encode(raw).decode('ascii')})

    def release_older(self, model):
        request_id = self.held.pop('older')
        raw = json.dumps(model, ensure_ascii=False).encode('utf-8')
        self._send('Fetch.fulfillRequest', {
            'requestId': request_id, 'responseCode': 200,
            'responseHeaders': [{'name': 'Content-Type', 'value': 'application/json; charset=utf-8'}],
            'body': base64.b64encode(raw).decode('ascii')})

    def evaluate(self, expression):
        result = self.call('Runtime.evaluate', {'expression': expression,
                            'returnByValue': True, 'awaitPromise': False})
        if 'exceptionDetails' in result:
            raise AssertionError('browser JS evaluation exception')
        return result.get('result', {}).get('value')

    def wait(self, expression, predicate, label, seconds=12):
        end = min(self.deadline.end, time.monotonic() + seconds)
        last = None
        while time.monotonic() < end:
            last = self.evaluate(expression)
            if predicate(last):
                return last
            self.pump(.08)
        raise AcceptanceTimeout(label + ' did not reach expected DOM state')

    def viewport(self, width, height):
        self.call('Emulation.setDeviceMetricsOverride', {
            'width': width, 'height': height, 'deviceScaleFactor': 1,
            'mobile': width == 390, 'screenWidth': width, 'screenHeight': height})

    def navigate(self):
        result = self.call('Page.navigate', {'url': self.origin + '/'})
        if result.get('errorText'):
            raise AssertionError('real document navigation failed')
        self.wait('document.readyState', lambda v: v == 'complete', 'page navigation')

    def screenshot(self, filename):
        result = self.call('Page.captureScreenshot', {
            'format': 'png', 'fromSurface': True, 'captureBeyondViewport': False})
        raw = base64.b64decode(result['data'], validate=True)
        if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
            raise AssertionError('Chromium screenshot is not PNG')
        filename.write_bytes(raw)
        return {'path': str(filename), 'sha256': hashlib.sha256(raw).hexdigest(),
                'pixel_size': list(struct.unpack('>II', raw[16:24]))}

    def close(self):
        failures = []
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                failures.append('CDP socket close')
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    try:
                        self.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        failures.append('browser process still running')
        return failures


DOM = r"""(()=>{
 const root=document.documentElement;
 const cards=[...document.querySelectorAll('#arms article')].map(a=>({
   name:a.querySelector('h3')?.textContent,
   fields:Object.fromEntries([...a.querySelectorAll('dl > div')].map(e=>
     [e.querySelector('dt')?.textContent,{value:e.querySelector('dd')?.textContent,
                                          exact:e.querySelector('dd')?.title||null}]))
 }));
 return {
  innerWidth:innerWidth,clientWidth:root.clientWidth,scrollWidth:root.scrollWidth,
  bodyScrollWidth:document.body.scrollWidth,
  cards:cards, freshness:document.getElementById('freshness').textContent,
  reasons:document.getElementById('reasons').textContent,
  journal:document.getElementById('journal').textContent,
  commitments:document.getElementById('commitments').textContent,
  pageText:document.body.textContent,
  injectionElements:document.querySelectorAll(
    '#arms img,#arms script,#journal img,#journal script,#commitments img,#commitments script').length,
  marker:window.__d2_attacked||false,
  oldDone:window.__d2_oldDone||false,
  recentDone:window.__d2_recentDone||false
 };
})()"""


def require(condition, reason):
    if not condition:
        raise AssertionError(reason)


def populated(cdp, fixture):
    return cdp.wait(DOM, lambda x: x and
                    any(a['name'] == 'ZERO-ARM' for a in x['cards']) and
                    fixture['hostile'] in x['pageText'] and
                    '記分卡：新鮮' in x['freshness'], 'populated real D2 DOM')


def run_probe(report, name, action):
    started = time.monotonic()
    record = {'id': name, 'status': 'NOT_RUN', 'duration_ms': 0, 'evidence': None}
    report['probes'][name] = record
    try:
        record['evidence'] = action()
        record['status'] = 'PASS'
    except AcceptanceTimeout as exc:
        record.update(status='TIMEOUT', evidence=str(exc)[:300])
    except Exception as exc:
        record.update(status='FAIL', evidence=(type(exc).__name__ + ': ' + str(exc))[:300])
    finally:
        record['duration_ms'] = round((time.monotonic() - started) * 1000, 2)
    return record['status'] == 'PASS'


def populated_probe(cdp, fixture, output, report):
    views = {}
    for width, height in ((1280, 800), (390, 844)):
        cdp.viewport(width, height)
        cdp.navigate()
        snap = populated(cdp, fixture)
        require(snap['innerWidth'] == width and snap['clientWidth'] == width,
                'CSS viewport/client mismatch at ' + str(width))
        require(snap['scrollWidth'] <= snap['clientWidth'] and
                snap['bodyScrollWidth'] <= snap['clientWidth'],
                'root/body horizontal overflow at ' + str(width))
        require(len(snap['cards']) == 2, 'synthetic arm count mismatch')
        require(fixture['long_text'] in snap['pageText'], 'long text missing')
        require('研究標題' in snap['journal'] and 'C-OPEN' in snap['commitments'],
                'actual journal/commitments not populated')
        screenshot = cdp.screenshot(output / ('d2-%dx%d.png' % (width, height)))
        require(screenshot['pixel_size'] == [width, height], 'screenshot geometry mismatch')
        views[str(width)] = {'metrics': {'innerWidth': snap['innerWidth'],
                             'clientWidth': snap['clientWidth'],
                             'scrollWidth': snap['scrollWidth'],
                             'bodyScrollWidth': snap['bodyScrollWidth']},
                             'screenshot': screenshot}
        report['artifacts'].append(screenshot)
    return views


def hostile_probe(cdp, fixture):
    snap = populated(cdp, fixture)
    require(snap['injectionElements'] == 0 and snap['marker'] is False,
            'hostile fixture created executable elements or event side effects')
    require(fixture['hostile'] in snap['cards'][1]['name'], 'hostile arm not literal')
    require(fixture['hostile'] in snap['journal'], 'hostile journal/verdict not literal')
    require(fixture['hostile'] in snap['commitments'], 'hostile commitment not literal')
    return {'hostile_text_literal': True, 'injected_elements': snap['injectionElements'],
            'event_marker': snap['marker']}


def unknown_zero_probe(cdp, fixture):
    cards = populated(cdp, fixture)['cards']
    zero, unknown = cards[0]['fields'], cards[1]['fields']
    require(cards[0]['name'] == 'ZERO-ARM', 'known zero arm missing')
    require(zero['訊號']['value'] == '0' and zero['淨損益']['value'] == '0.00',
            'known zero was not displayed numerically')
    require(zero['淨損益']['exact'] == '0', 'known exact zero lost')
    require(unknown['訊號']['value'] == '未知' and unknown['淨損益']['value'] == '未知'
            and unknown['已經天數']['value'] == '未知', 'null values coerced to zero')
    require(unknown['淨損益']['exact'] is None, 'unknown money has an exact value')
    return {'known': {'signals': zero['訊號'], 'pnl': zero['淨損益']},
            'unknown': {'signals': unknown['訊號'], 'pnl': unknown['淨損益'],
                        'days_in': unknown['已經天數']}}


def refresh(cdp):
    cdp.evaluate('void window.refreshDashboard()')


def failure_probe(cdp, fixture, expected_model):
    results = {}
    for mode in ('status', 'invalid_json', 'invalid_model', 'network_fail'):
        require(len(populated(cdp, fixture)['cards']) == 2, 'not populated before fault')
        cdp.inject.append(mode)
        refresh(cdp)
        snap = cdp.wait(DOM, lambda x: x and not x['cards'] and
                        x['pageText'].find('未知') >= 0 and
                        ('讀取失敗' in x['reasons'] or '資料格式無效' in x['reasons']),
                        mode + ' prior-value clearing')
        require('ZERO-ARM' not in snap['pageText'], 'prior arm persisted after ' + mode)
        results[mode] = {'cleared_arms': True, 'reason': snap['reasons']}
        refresh(cdp)
        populated(cdp, fixture)
    # The older HTTP response is genuinely delayed at CDP request interception.
    cdp.inject.append('hold')
    cdp.evaluate('window.__d2_oldDone=false;window.refreshDashboard().then(()=>window.__d2_oldDone=true)')
    cdp.wait('Boolean(' + "'older'" + ' in window)', lambda _: False,
             'disabled', seconds=.001) if False else None
    end = time.monotonic() + cdp.deadline.left(6)
    while 'older' not in cdp.held and time.monotonic() < end:
        cdp.pump(.1)
    require('older' in cdp.held, 'older response was not held')
    cdp.evaluate('window.__d2_recentDone=false;window.refreshDashboard().then(()=>window.__d2_recentDone=true)')
    cdp.wait(DOM, lambda x: x and x['recentDone'] and
             any(a['name'] == 'ZERO-ARM' for a in x['cards']), 'newer request did not finish')
    old = json.loads(json.dumps(expected_model, ensure_ascii=False))
    old['arms'][0]['arm'] = 'STALE_RESPONSE_NOT_ALLOWED'
    cdp.release_older(old)
    snap = cdp.wait(DOM, lambda x: x and x['oldDone'], 'old response did not finish')
    require('STALE_RESPONSE_NOT_ALLOWED' not in snap['pageText'] and
            any(a['name'] == 'ZERO-ARM' for a in snap['cards']),
            'older response replaced a newer result')
    results['older_response_ordering'] = {'delayed_older': True, 'stale_not_rendered': True}
    return results


def api_json(origin):
    with urlopen(origin + '/api/dashboard', timeout=3) as response:
        require(response.status == 200, 'API was not HTTP 200')
        return json.loads(response.read(1048576))


def exit_for(report):
    statuses = [record['status'] for record in report['probes'].values()]
    if report.get('cleanup') != 'CLEAN':
        return 1
    if 'TIMEOUT' in statuses:
        return 3
    if 'FAIL' in statuses:
        return 1
    if len(statuses) != len(PROBES) or 'NOT_RUN' in statuses:
        return 2
    return 0


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--expected-head', required=True, help='operator-verified 40-character checkout HEAD')
    p.add_argument('--browser', help='explicit preinstalled Chrome/Chromium executable')
    p.add_argument('--deadline-seconds', type=int, default=120)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if not 30 <= args.deadline_seconds <= 240:
        parser().error('deadline-seconds must be 30..240')
    deadline = Deadline(args.deadline_seconds)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {'schema_version': 1, 'classification': 'SYNTHETIC_REAL_D2_BROWSER_ACCEPTANCE',
              'instruction_id': 'D2-BROWSER-ENTRY-r0',
              'command': [sys.executable, str(Path(__file__).resolve()),
                          *(sys.argv[1:] if argv is None else argv)],
              'config': {'expected_head': args.expected_head, 'browser_override': args.browser,
                         'deadline_seconds': args.deadline_seconds, 'output': str(output)},
              'checkout_head': None, 'input_state_before': None, 'input_state_after': None,
              'probes': {name: {'id': name, 'status': 'NOT_RUN', 'duration_ms': 0,
                                'evidence': 'prerequisite not reached'} for name in PROBES},
              'artifacts': [], 'network_trace': [], 'network_violations': [],
              'readonly_reads': 0, 'cleanup': 'NOT_RUN', 'failures': [], 'exit_code': 2}
    httpd = None
    thread = None
    cdp = None
    temp = None
    try:
        if not re.fullmatch('[0-9a-f]{40}', args.expected_head):
            raise ValueError('expected-head must be full lowercase SHA')
        report['checkout_head'] = git_head()
        require(report['checkout_head'] == args.expected_head, 'checkout HEAD mismatch')
        fixture = fixture_contract(json.loads(FIXTURE.read_text(encoding='utf-8')))
        chrome = browser_binary(args.browser)
        if not chrome:
            raise FileNotFoundError('preinstalled real Chromium executable unavailable')
        report['browser_name'] = Path(chrome).name
        temp = tempfile.TemporaryDirectory(prefix='d2-browser-acceptance-')
        root = Path(temp.name)
        paths = write_sources(root, fixture)
        tracked = dict(TRACKED)
        tracked.update({'synthetic_' + str(i): p for i, p in enumerate(paths)})
        report['input_state_before'] = state(tracked)
        httpd = server.create_server(scorecard_path=paths[0], journal_path=paths[1],
                                     ledger_path=paths[2], port=0,
                                     max_age_ms=1000, clock_ms=lambda: fixture['now_ms'])
        require(httpd.server_address[0] == '127.0.0.1', 'server is not loopback-only')
        thread = threading.Thread(target=httpd.serve_forever,
                                  kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        origin = 'http://127.0.0.1:' + str(httpd.server_port)
        report['origin'] = origin
        model = api_json(origin)
        expected = read_dashboard(*paths, now_ms=fixture['now_ms'], max_age_ms=1000)
        require(model == expected and model['scorecard_state'] == 'known',
                'real HTTP model and synthetic reader mismatch')
        cdp = ChromeCDP(chrome, root / 'chrome-profile', origin, deadline)
        run_probe(report, PROBES[0], lambda: populated_probe(cdp, fixture, output, report))
        run_probe(report, PROBES[1], lambda: hostile_probe(cdp, fixture))
        run_probe(report, PROBES[2], lambda: unknown_zero_probe(cdp, fixture))
        run_probe(report, PROBES[3], lambda: failure_probe(cdp, fixture, model))
        def audit_probe():
            # Drain request and failure events, including requests blocked preflight.
            for _ in range(8):
                cdp.pump(.05)
            require(not cdp.violations, 'non-permitted browser requests attempted')
            return {'attempted_requests': len(cdp.trace), 'disallowed': 0,
                    'failed_requests': len(cdp.failed_loads),
                    'lifecycle_events': sorted({entry['event'] for entry in cdp.trace})}
        run_probe(report, PROBES[4], audit_probe)
        report['network_trace'] = cdp.trace
        report['network_violations'] = cdp.violations
        for _ in range(100):
            require(api_json(origin) == model, 'read-only API result changed across 100 reads')
            report['readonly_reads'] += 1
        report['input_state_after'] = state(tracked)
        require(report['input_state_before'] == report['input_state_after'],
                'source/asset bytes, SHA256 or mtime changed')
    except AcceptanceTimeout as exc:
        report['failures'].append('TIMEOUT: ' + str(exc)[:300])
        for record in report['probes'].values():
            if record['status'] == 'NOT_RUN':
                record['status'] = 'TIMEOUT'
                record['evidence'] = 'deadline reached before this probe'
    except Exception as exc:
        report['failures'].append(type(exc).__name__ + ': ' + str(exc)[:300])
    finally:
        cleanup_failures = []
        if cdp is not None:
            cleanup_failures.extend(cdp.close())
            report['network_trace'] = cdp.trace
            report['network_violations'] = cdp.violations
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                cleanup_failures.append('loopback server shutdown')
        if thread is not None:
            thread.join(timeout=3)
            if thread.is_alive():
                cleanup_failures.append('loopback server thread still running')
        if temp is not None:
            try:
                temp.cleanup()
            except OSError:
                cleanup_failures.append('temporary directory cleanup')
        report['cleanup'] = 'CLEAN' if not cleanup_failures else 'FAIL'
        report['failures'].extend(cleanup_failures)
        if report['failures'] and all(r['status'] == 'NOT_RUN' for r in report['probes'].values()):
            report['status'] = 'BLOCKED'
        else:
            report['status'] = 'COMPLETE'
        report['exit_code'] = exit_for(report)
        if report['failures'] and report['exit_code'] == 0:
            report['exit_code'] = 1
        report_path = output / 'report.json'
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return report['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
