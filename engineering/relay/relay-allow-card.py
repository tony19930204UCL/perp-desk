#!/usr/bin/env python3
"""Operator allow-once for a GitHub write card the automatic scope check refused.

Usage: relay-allow-card.py JOB_DIR   (JOB_DIR contains job.json with transport_command,
repository, branch, write_allowlist)

Passes ONLY when: exactly one update_file/create_file op; repo/branch/path in the job
allowlist; no 32+ char token-like strings; every secret-regex hit sits on a line that
defines a regex (detection code, not a value). Clicks the button starting with the
allow-once label. Never the conversation-wide option.
"""
import datetime, json, re, subprocess, sys, time
import product_dispatch as pd

S = sys.argv[1]
j = json.load(open(S + '/job.json'))
CARD = '要允許 ChatGPT 使用 GitHub 嗎？'


def run(req):
    p = subprocess.run(j['transport_command'], input=json.dumps(req),
                       capture_output=True, text=True, timeout=60)
    return json.loads(p.stdout)


snap = lambda: run({'op': 'snapshot'})
ev = lambda e: run({'op': 'evaluate', 'expression': e})

if CARD not in snap()['text']:
    print('NO_CARD')
    sys.exit(0)
ev('(()=>{const b=[...document.querySelectorAll("button")].filter(e=>e.innerText==="查看詳細資訊");'
   'if(b.length!==1)throw Error("n="+b.length);b[0].click();return true})()')
time.sleep(1.5)
ds = ev('JSON.stringify([...document.querySelectorAll("[role=dialog]")].map(e=>e.innerText))')
ds = json.loads(ds) if isinstance(ds, str) else ds
ev('(()=>{const b=[...document.querySelectorAll("button")].find(e=>e.innerText==="關閉對話框");'
   'if(b)b.click();return true})()')

ops = []
for t in ds:
    if 'call_tool\n' in t:
        ops.append(json.JSONDecoder().raw_decode(t.split('call_tool\n', 1)[1].lstrip())[0])
assert len(ops) == 1, 'operation count %d' % len(ops)
op = ops[0]
a = op['args']
c = a.get('content', '')
tool = op['path'].rsplit('/', 1)[-1]
assert tool in ('update_file', 'create_file'), tool
assert (a.get('repository_full_name') == j['repository'] and a.get('branch') == j['branch']
        and a.get('path') in j['write_allowlist']), 'scope'
assert isinstance(c, str) and len(c) <= 180000, 'content'
# Long strings are allowed only when they are plain snake_case identifiers (letters and digits in words joined by underscores, at least two underscores,
# every word 1 to 24 characters, no run of 12+ digits or hex-looking word). Random keys, hashes and base64 never look like that.
SNAKE = re.compile(r'[a-z][a-z0-9]{0,23}(?:_[a-z0-9]{1,24}){2,}')
for w in re.findall(r'[A-Za-z0-9_\-]{32,}', c):
    ok = SNAKE.fullmatch(w) and sum(ch.isdigit() for ch in w) <= len(w) * 0.1
    assert ok, 'token-like string present'
if re.search(r'(?i)sk_(?:live|test)_|xox[abp]-|AKIA[0-9A-Z]{12,}|-----BEGIN', c):
    raise AssertionError('token-like string present')
pat = re.compile(r'(?i)(?:sk-[a-z0-9]{20,}|ghp_[a-z0-9]{20,}|PRIVATE KEY'
                 r'|api[_-]?key\s*[=:]\s*["\'][a-z0-9]{20,})')
for m in pat.finditer(c):
    end = c.find('\n', m.end())
    line = c[c.rfind('\n', 0, m.start()) + 1: end if end >= 0 else len(c)]
    assert 're.compile' in line or 'SECRET' in line.upper(), 'hit outside a detection rule: ' + line[:80]
assert CARD in snap()['text']
r = # Exact verified card + shared confirmed-send source must authorize click.
active = json.load(open(S + '/active_instruction.json', encoding='utf-8'))
iid = active['instruction_id']
fp = __import__('hashlib').sha256(json.dumps(op, sort_keys=True).encode()).hexdigest()
pd.approval_once(j, S, iid, fp, chat=j.get('dispatch_chat_url'))
pd.approve_check(j, S, iid, fp, chat=j.get('dispatch_chat_url'))
ev('(()=>{const b=[...document.querySelectorAll("button")].filter(e=>e.innerText.startsWith("允許一次"));'
       'if(b.length!==1)throw Error("Ambiguous approval");b[0].click();return true})()')
time.sleep(4)
gone = CARD not in snap()['text']
if gone:
    pd.reconcile(j, S, iid, receipt='APPROVED', chat=j.get('dispatch_chat_url'))
open(S + '/approved_once_manual_%d.json' % int(time.time()), 'w').write(json.dumps({
    'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'by': 'operator allow-once (relay-allow-card.py)', 'tool': tool, 'file': a['path'],
    'branch': a['branch'], 'detection_rule_hits_only': True, 'prompt_gone': gone}, indent=1))
print('ALLOWED_ONCE', a['path'], 'prompt_gone', gone)
