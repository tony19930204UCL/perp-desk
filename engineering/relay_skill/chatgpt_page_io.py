#!/usr/bin/env python3
"""chatgpt_page_io.py - WSL controller for the dedicated Windows ChatGPT Chrome.

Transport (measured): Windows-native `node.exe -` with the program on stdin,
page-level CDP only (HTTP /json/list + the target's own webSocketDebuggerUrl).
Browser-level Playwright connectOverCDP is NOT used (it times out).
The Node program is a fixed constant; parameters travel as a JSON envelope FILE
whose Windows path is process.argv[2]. No string splicing of prompts or quotes.

Subcommands: verify-owner | list-targets | send | collect | approve-card (list only)

Safety rules encoded here (and unit-tested as pure logic):
 * every browser operation holds the lab flock (cache/auto_allow.lock);
 * listener on the CDP port must be loopback + chrome.exe + dedicated profile;
 * send is a receipt state machine; after SEND_INTENT nothing is retried or resent;
 * mechanical retries (max 3) only for pre-send phases and only for a Windows
   process-launch failure (accept4 failed 110 and friends), or for read-only phases;
 * approval cards are never clicked; 'Regenerate' is never clicked.
"""
import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
import product_dispatch as pd
import relay_fixes as fixes
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PORT = 19242
NODE_EXE = "/mnt/c/Program Files/nodejs/node.exe"
WIN_WORKDIR = r"<WINDOWS_WORKDIR>"
LOOPBACK = {"127.0.0.1", "::1"}
MAX_ATTEMPTS = 3

# ---------------------------------------------------------------- pure logic

STAGES = ["NEW", "DRAFT_VERIFIED", "SEND_INTENT", "CLICK_ACK", "CONFIRMED", "SEND_UNKNOWN"]
POST_INTENT = {"SEND_INTENT", "CLICK_ACK", "CONFIRMED", "SEND_UNKNOWN"}
TERMINAL = {"CONFIRMED", "SEND_UNKNOWN", "DRY_RUN_CLEARED", "PRESEND_FAILED"}
_ALLOWED = {
    "NEW": {"DRAFT_VERIFIED", "PRESEND_FAILED"},
    "DRAFT_VERIFIED": {"SEND_INTENT", "DRY_RUN_CLEARED", "PRESEND_FAILED"},
    "SEND_INTENT": {"CLICK_ACK", "SEND_UNKNOWN"},
    "CLICK_ACK": {"CONFIRMED", "SEND_UNKNOWN"},
    "CONFIRMED": set(),
    "SEND_UNKNOWN": set(),
    "DRY_RUN_CLEARED": set(),
    "PRESEND_FAILED": set(),
}
RETRYABLE_PHASES = {"login", "verify", "list", "open_new", "spa_goto", "prepare", "snapshot", "readback", "cards", "reload", "close", "clear"}
PRESEND_PHASES = {"verify", "list", "open_new", "spa_goto", "prepare", "clear", "close"}
LAUNCH_FAILURE = re.compile(r"accept4|UtilAcceptVsock|failed 110|Connection timed out|Input/output error|Exec format", re.I)

_DECOR = re.compile(r"(?:\n?[ \t]*…[ \t]*)?\n?[ \t]*(?:顯示較多|顯示較少|Show more|Show less)[ \t]*$")


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_bubble(text):
    """Normalise a rendered user bubble / prompt for comparison.
    Strips the trailing 'Show more' decoration (ellipsis + label), CRLF, trailing
    whitespace on each line and trailing newlines. Interior text is untouched."""
    if text is None:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = t.rstrip()
    for _ in range(2):
        t2 = _DECOR.sub("", t).rstrip()
        if t2 == t:
            break
        t = t2
    return "\n".join(line.rstrip() for line in t.split("\n"))


def bubble_equals(prompt, bubble):
    """bubble = {'pre': str|None, 'full': str}; either rendering may equal the prompt."""
    return fixes.bubble_match(prompt, bubble)


def count_matching(prompt, bubbles):
    return sum(1 for b in bubbles if bubble_equals(prompt, b))


def classify_readback(prompt, bubbles, baseline_matches):
    """CONFIRMED only if exactly one NEW bubble equals the prompt; never guess."""
    n = count_matching(prompt, bubbles)
    if n == baseline_matches + 1:
        return "CONFIRMED"
    if n == baseline_matches:
        return "PENDING"
    return "AMBIGUOUS"


def owner_ok(entries):
    """entries: list of {addr, pid, name, dedicated}. All must be loopback chrome.exe of the dedicated profile."""
    if not entries:
        return False, "NO_LISTENER"
    for e in entries:
        if str(e.get("addr")) not in LOOPBACK:
            return False, "NOT_LOOPBACK:%s" % e.get("addr")
        if str(e.get("name", "")).lower() != "chrome.exe":
            return False, "OWNER_NOT_CHROME:%s" % e.get("name")
        if not e.get("dedicated"):
            return False, "NOT_DEDICATED_PROFILE"
    return True, "OK"


def should_retry(phase, attempt, launch_failure, mode_readonly=False):
    """Mechanical retry policy. attempt is 1-based number of attempts already made.
    - click phase: never.
    - pre-send phases: only when the Windows process never started (launch failure).
    - read-only phases: any failure."""
    if attempt >= MAX_ATTEMPTS:
        return False
    if phase == "click":
        return False
    if phase in PRESEND_PHASES:
        return bool(launch_failure)
    if phase in RETRYABLE_PHASES:
        return True
    return False


def is_launch_failure(rc, stdout, stderr):
    if "RELAY_RESULT:" in (stdout or ""):
        return False
    return rc != 0 and bool(LAUNCH_FAILURE.search((stderr or "") + (stdout or "")) or not (stdout or "").strip())


class ReceiptError(Exception):
    pass


class Receipt:
    """Send receipt state machine persisted as JSON. SEND_INTENT is written (fsync) BEFORE the click."""

    def __init__(self, path, instruction_id, prompt_sha256="", prompt_len=0):
        self.path = Path(path)
        self.data = {
            "instruction_id": instruction_id, "stage": "NEW", "submit_attempts": 0, "submit_count": 0,
            "prompt_sha256": prompt_sha256, "prompt_len": prompt_len, "target": None, "url": None,
            "history": [{"stage": "NEW", "at": now_utc()}],
        }

    @classmethod
    def load(cls, path):
        r = cls(path, "")
        r.data = json.loads(Path(path).read_text(encoding="utf-8"))
        return r

    @property
    def stage(self):
        return self.data["stage"]

    def can_send(self):
        return self.stage not in POST_INTENT

    def advance(self, stage, **info):
        if stage not in _ALLOWED.get(self.stage, set()):
            raise ReceiptError("illegal transition %s -> %s" % (self.stage, stage))
        self.data["stage"] = stage
        if stage == "SEND_INTENT":
            self.data["submit_attempts"] += 1
        if stage == "CLICK_ACK":
            self.data["submit_count"] += 1
        for k in ("target", "url"):
            if info.get(k):
                self.data[k] = info[k]
        self.data["history"].append(dict({"stage": stage, "at": now_utc()}, **info))
        self.save()

    def update(self, **info):
        self.data.update(info)
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)


END_FAIL = ("無法載入這則回覆", "連線已中斷", "couldn't load", "Connection lost")
_FOOTER = re.compile(r"\n?ChatGPT 可能會出錯.*$", re.S)
ANSWER_MARK = "ChatGPT 說："


def extract_answer(text):
    """Assistant text after the last 'ChatGPT 說：' marker, footer removed."""
    i = text.rfind(ANSWER_MARK)
    if i < 0:
        return ""
    ans = text[i + len(ANSWER_MARK):].lstrip("\n")
    ans = _FOOTER.sub("", ans)
    return ans.rstrip()


def analyze_collect(snap, end_marker=None):
    """Return dict(status, answer). status in GENERATING | LOAD_FAILED | COMPLETE_END | IDLE | NO_ANSWER."""
    ans = extract_answer(snap.get("text", ""))
    busy = bool(snap.get("busy"))
    if end_marker and end_marker in ans:
        cut = ans.rfind(end_marker) + len(end_marker)
        return {"status": "COMPLETE_END", "answer": ans[:cut], "busy": busy}
    if busy:
        return {"status": "GENERATING", "answer": ans, "busy": busy}
    if any(m in ans for m in END_FAIL) and len(ans) < 400:
        return {"status": "LOAD_FAILED", "answer": ans, "busy": busy}
    if not ans:
        return {"status": "NO_ANSWER", "answer": ans, "busy": busy}
    return {"status": "IDLE", "answer": ans, "busy": busy}


class Stability:
    """Consecutive identical answers (sha) counter."""

    def __init__(self):
        self.last = None
        self.n = 0

    def feed(self, answer):
        h = hashlib.sha256(answer.encode("utf-8")).hexdigest()
        self.n = self.n + 1 if h == self.last else 1
        self.last = h
        return self.n


# ---------------------------------------------------------------- node program (constant)

NODE_PROGRAM = r"""
'use strict';
const fs = require('fs');
const cp = require('child_process');
const ENV = JSON.parse(fs.readFileSync(process.argv[2], 'utf8').replace(/^\uFEFF/, ''));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const BASE = 'http://127.0.0.1:' + ENV.port;
const out = o => { process.stdout.write('RELAY_RESULT:' + JSON.stringify(o) + '\n'); };

const PS_OWNER = [
  "$ErrorActionPreference='Stop'",
  "$p=[int]$env:RELAY_PORT",
  "$c=@(Get-NetTCPConnection -LocalPort $p -State Listen)",
  "$o=@($c|ForEach-Object{ $pr=Get-CimInstance Win32_Process -Filter ('ProcessId='+$_.OwningProcess); [pscustomobject]@{addr=[string]$_.LocalAddress;pid=[int]$_.OwningProcess;name=[string]$pr.Name;dedicated=[bool]($pr.CommandLine -like '*ChatGPTDedicatedChrome*')} })",
  "ConvertTo-Json -InputObject @($o) -Compress"
].join('\n');

async function listTargets() {
  const r = await fetch(BASE + '/json/list', { signal: AbortSignal.timeout(8000) });
  return await r.json();
}
function openWs(url, timeoutMs) {
  return new Promise((res, rej) => {
    const w = new WebSocket(url); let id = 0; const pend = new Map();
    const timer = setTimeout(() => { try { w.close(); } catch (e) {} rej(new Error('WS_CONNECT_TIMEOUT')); }, timeoutMs);
    w.onopen = () => { clearTimeout(timer); res({
      call: (method, params, t) => new Promise((rs, rj) => {
        const i = ++id; const tm = setTimeout(() => { pend.delete(i); rj(new Error('CDP_TIMEOUT ' + method)); }, t || 20000);
        pend.set(i, { rs, rj, tm }); w.send(JSON.stringify({ id: i, method, params: params || {} })); }),
      close: () => { try { w.close(); } catch (e) {} } }); };
    w.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { const p = pend.get(m.id); pend.delete(m.id); clearTimeout(p.tm);
      if (m.error) p.rj(new Error('CDP_ERROR ' + JSON.stringify(m.error))); else p.rs(m.result); } };
    w.onerror = () => { clearTimeout(timer); rej(new Error('WS_ERROR')); };
  });
}
async function evalIn(page, fn, arg, timeoutMs) {
  const expr = '(' + fn.toString() + ')(' + JSON.stringify(arg === undefined ? null : arg) + ')';
  const r = await page.call('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true }, timeoutMs);
  if (r.exceptionDetails) throw new Error('EVAL_EXCEPTION ' + JSON.stringify((r.exceptionDetails.exception && r.exceptionDetails.exception.description) || r.exceptionDetails.text));
  return r.result.value;
}
// A hidden (background) tab is frozen: Runtime.evaluate hangs. Mutating ops bring the tab to front first;
// read-only ops try once and only bring it to front when the evaluate times out.
async function withTarget(id, fn, front) {
  const list = await listTargets();
  const t = list.find(x => x.id === id && x.type === 'page');
  if (!t) throw new Error('TARGET_NOT_FOUND');
  const page = await openWs(t.webSocketDebuggerUrl, 10000);
  try {
    if (front === 'always') { await page.call('Page.bringToFront'); await sleep(600); }
    else if (front === 'ifhidden') {
      try { await evalIn(page, function () { return document.visibilityState; }, null, 3000); }
      catch (e) { if (!/CDP_TIMEOUT/.test(String(e))) throw e; await page.call('Page.bringToFront'); await sleep(800); }
    }
    return await fn(page, t);
  } finally { page.close(); }
}

// ---- page-side functions (self-contained; serialised with fn.toString()) ----
function PAGE_STATE() {
  const q = (s, r) => Array.from((r || document).querySelectorAll(s));
  const inBubble = e => !!e.closest('[data-user-message-bubble]');
  const boxes = q('[contenteditable="true"][role="textbox"]').filter(e => !inBubble(e));
  const comp = boxes.length === 1 ? boxes[0] : null;
  const ctext = comp ? Array.from(comp.children).map(p => p.textContent).join('\n') : null;
  const bubbles = q('[data-user-message-bubble]').map(b => {
    const pre = q('.whitespace-pre-wrap', b)[0];
    const roots = q('[data-message-author-role="user"]');
    const bound = roots.filter(t => t.contains(b));
    // Inspect the complete bound message tree, not truncated preview text.
    const copy = b.cloneNode(true);
    copy.querySelectorAll('button,[role="button"],[aria-expanded],svg')
        .forEach(x => x.remove());
    return { pre: pre ? pre.textContent : null, full: b.innerText,
      turn_bound: bound.length === 1, subtree: copy.textContent };
  });
  const btns = q('button').filter(b => !b.closest('nav,aside'));
  const lab = b => (b.getAttribute('aria-label') || '') + '|' + (b.getAttribute('data-testid') || '');
  const send = btns.filter(b => /send-button|^(傳送|Send)/i.test(lab(b)));
  const stop = btns.filter(b => /stop-button|^(停止|Stop)/i.test(lab(b)));
  return { url: location.href, ready: document.readyState, boxes: boxes.length, composer: ctext,
    bubbles: bubbles, send: send.map(b => ({ label: lab(b), disabled: !!b.disabled })), busy: stop.length > 0,
    text: document.body.innerText, labels: btns.map(lab).filter(x => x !== '|').slice(0, 60) };
}
function PAGE_INSERT(arg) {
  const boxes = Array.from(document.querySelectorAll('[contenteditable="true"][role="textbox"]')).filter(e => !e.closest('[data-user-message-bubble]'));
  if (boxes.length !== 1) return { ok: false, why: 'COMPOSER_COUNT_' + boxes.length };
  const comp = boxes[0]; comp.focus();
  const cur = Array.from(comp.children).map(p => p.textContent).join('\n');
  if (cur !== '') return { ok: false, why: 'COMPOSER_NOT_EMPTY' };
  const ok = document.execCommand('insertText', false, arg.text);
  return { ok: ok, why: ok ? 'INSERTED' : 'INSERT_REJECTED' };
}
function PAGE_CLEAR() {
  const boxes = Array.from(document.querySelectorAll('[contenteditable="true"][role="textbox"]')).filter(e => !e.closest('[data-user-message-bubble]'));
  if (boxes.length !== 1) return { ok: false, why: 'COMPOSER_COUNT_' + boxes.length };
  const comp = boxes[0]; comp.focus();
  document.execCommand('selectAll', false, null); document.execCommand('delete', false, null);
  return { ok: true };
}
function PAGE_CLICK_SEND() {
  const btns = Array.from(document.querySelectorAll('button')).filter(b => !b.closest('nav,aside'));
  const lab = b => (b.getAttribute('aria-label') || '') + '|' + (b.getAttribute('data-testid') || '');
  const send = btns.filter(b => /send-button|^(傳送|Send)/i.test(lab(b)) && !b.disabled);
  if (send.length !== 1) return { ok: false, why: 'SEND_BUTTON_COUNT_' + send.length };
  send[0].click();
  return { ok: true, label: lab(send[0]) };
}
function PAGE_SPA(arg) {
  const as = Array.from(document.querySelectorAll('a[href]')).filter(a => a.getAttribute('href') === arg.path);
  let pick = as;
  if (arg.path === '/') {
    const nc = as.filter(a => /新對話|New chat/i.test((a.getAttribute('aria-label') || '') + (a.innerText || '')));
    if (nc.length) pick = nc;
    if (!pick.length) pick = Array.from(document.querySelectorAll('button')).filter(b => /^(新對話|New chat)/i.test(b.getAttribute('aria-label') || ''));
  }
  if (pick.length < 1) return { ok: false, why: 'ANCHOR_COUNT_0' };
  pick[0].click();
  return { ok: true, n: pick.length };
}
async function PAGE_SESSION() {
  const r = await fetch('/api/auth/session', { credentials: 'include' });
  const j = await r.json().catch(() => ({}));
  return { status: r.status, logged_in: !!(j && j.user) };   // never return tokens
}
function PAGE_CARDS() {
  const out = [];
  const btns = Array.from(document.querySelectorAll('button')).filter(b => !b.closest('nav,aside'));
  for (const b of btns) {
    const t = (b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '');
    if (/允許|核准|Allow|Approve|拒絕|Deny|查看詳細資訊|在 ChatGPT Work 中繼續|使用「工作」模式|留在「對話」/i.test(t)) {
      let c = b; for (let i = 0; i < 4 && c.parentElement; i++) c = c.parentElement;
      out.push({ button: t.trim().slice(0, 60), context: (c.innerText || '').slice(0, 400) });
    }
  }
  return out;
}

const OPS = {
  async verify_owner() {
    const raw = cp.execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', PS_OWNER],
      { env: Object.assign({}, process.env, { RELAY_PORT: String(ENV.port) }), timeout: 30000, encoding: 'utf8' });
    return { entries: JSON.parse(raw.trim() || '[]') };
  },
  async list_targets() {
    const l = await listTargets();
    return { targets: l.filter(t => t.type === 'page' && /^https:\/\/chatgpt\.com\//.test(t.url)).map(t => ({ id: t.id, url: t.url, title: t.title })) };
  },
  async open_new() {
    const r = await fetch(BASE + '/json/new?' + ENV.url, { method: 'PUT', signal: AbortSignal.timeout(10000) });
    const t = await r.json();
    // a background tab never hydrates the home composer: bring OUR new tab to the front
    try { await withTarget(t.id, async (p) => { await p.call('Page.bringToFront'); }); } catch (e) {}
    for (let i = 0; i < 40; i++) {
      await sleep(500);
      try { const s = await withTarget(t.id, (p) => evalIn(p, PAGE_STATE)); if (s.ready === 'complete' && s.boxes === 1) return { id: t.id, url: s.url }; } catch (e) {}
    }
    return { id: t.id, url: t.url, warn: 'COMPOSER_NOT_READY' };
  },
  async snapshot() {
    return await withTarget(ENV.target, async (p, t) => { const s = await evalIn(p, PAGE_STATE); s.target = t.id; return s; }, 'ifhidden');
  },
  async cards() {
    return await withTarget(ENV.target, async (p, t) => ({ url: t.url, cards: await evalIn(p, PAGE_CARDS) }), 'ifhidden');
  },
  async prepare() {
    return await withTarget(ENV.target, async (p, t) => {
      let s = await evalIn(p, PAGE_STATE);
      const urlOk = ENV.new ? (/^https:\/\/chatgpt\.com\/?(\?.*)?$/.test(s.url)) : (s.url === ENV.chat_url);
      if (!urlOk) return { ok: false, why: 'URL_MISMATCH', url: s.url };
      if (s.boxes !== 1) return { ok: false, why: 'COMPOSER_COUNT_' + s.boxes };
      if (s.busy) return { ok: false, why: 'PAGE_BUSY_GENERATING' };
      let inserted = false;
      if (s.composer === '') {
        const r = await evalIn(p, PAGE_INSERT, { text: ENV.prompt });
        if (!r.ok) return { ok: false, why: r.why };
        inserted = true; await sleep(700);
        s = await evalIn(p, PAGE_STATE);
      } else if (s.composer !== ENV.prompt) {
        return { ok: false, why: 'COMPOSER_NOT_EMPTY' };
      }
      return { ok: true, draft_match: s.composer === ENV.prompt, inserted: inserted, url: s.url,
        composer_len: (s.composer || '').length, send: s.send, bubbles: s.bubbles, labels: s.labels };
    }, 'always');
  },
  async click() {
    return await withTarget(ENV.target, async (p, t) => {
      const s = await evalIn(p, PAGE_STATE);
      const urlOk = ENV.new ? (/^https:\/\/chatgpt\.com\/?(\?.*)?$/.test(s.url)) : (s.url === ENV.chat_url);
      if (!urlOk) return { ok: false, clicked: false, why: 'URL_MISMATCH' };
      if (s.composer !== ENV.prompt) return { ok: false, clicked: false, why: 'DRAFT_CHANGED' };
      const r = await evalIn(p, PAGE_CLICK_SEND);
      return Object.assign({ clicked: !!r.ok }, r);
    }, 'always');
  },
  async spa_goto() {
    // in-app navigation inside an already hydrated tab (fresh tabs currently never hydrate the composer)
    return await withTarget(ENV.target, async (p) => {
      const r = await evalIn(p, PAGE_SPA, { path: ENV.path });
      if (!r.ok) return r;
      for (let i = 0; i < 60; i++) {
        await sleep(500);
        const s = await evalIn(p, PAGE_STATE);
        const u = new URL(s.url);
        if (u.pathname === ENV.path && s.boxes === 1 && s.ready === 'complete') return { ok: true, url: s.url };
      }
      return { ok: false, why: 'SPA_NAV_TIMEOUT' };
    }, 'always');
  },
  async login_probe() {
    // temp tab of our own; reports only a boolean, never tokens; closes the tab afterwards
    const t = await (await fetch(BASE + '/json/new?https://chatgpt.com/auth/login', { method: 'PUT', signal: AbortSignal.timeout(10000) })).json();
    try { await sleep(3000); return await withTarget(t.id, async p => await evalIn(p, PAGE_SESSION), 'always'); }
    finally { try { await fetch(BASE + '/json/close/' + t.id); } catch (e) {} }
  },
  async clear() {
    return await withTarget(ENV.target, async (p) => {
      const s = await evalIn(p, PAGE_STATE);
      if (s.composer !== ENV.prompt && s.composer !== '') return { ok: false, why: 'COMPOSER_FOREIGN' };
      await evalIn(p, PAGE_CLEAR); await sleep(400);
      const s2 = await evalIn(p, PAGE_STATE);
      return { ok: s2.composer === '', composer_len: (s2.composer || '').length };
    }, 'always');
  },
  async close() {
    const r = await fetch(BASE + '/json/close/' + ENV.target, { signal: AbortSignal.timeout(8000) });
    return { status: await r.text() };
  },
  async reload() {
    return await withTarget(ENV.target, async (p, t) => {
      await p.call('Page.reload', { ignoreCache: false });
      await sleep(3000);
      for (let i = 0; i < 40; i++) {
        try { const s = await evalIn(p, PAGE_STATE); if (s.ready === 'complete' && s.text.length > 200) return { ok: true, url: s.url }; } catch (e) {}
        await sleep(1000);
      }
      return { ok: false, why: 'RELOAD_NOT_READY' };
    }, 'always');
  }
};

(async () => {
  try {
    const fn = OPS[ENV.op];
    if (!fn) throw new Error('UNKNOWN_OP ' + ENV.op);
    const res = await fn();
    out({ ok: true, op: ENV.op, result: res });
  } catch (e) {
    out({ ok: false, op: ENV.op, error: String(e && e.message || e) });
    process.exitCode = 1;
  }
})();
"""

# ---------------------------------------------------------------- controller


def hermes_home():
    h = os.environ.get("HERMES_HOME")
    if h:
        return Path(h)
    return Path(__file__).resolve().parents[3]


def win_to_wsl(p):
    m = re.match(r"^([A-Za-z]):\\(.*)$", p)
    return "/mnt/%s/%s" % (m.group(1).lower(), m.group(2).replace("\\", "/"))


@contextlib.contextmanager
def lab_lock(path, timeout=180):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError("LAB_LOCK_TIMEOUT %s" % path)
                time.sleep(1)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class Node:
    """Runs the constant NODE_PROGRAM via `node.exe -` (stdin) with a JSON envelope FILE (argv[2])."""

    def __init__(self, port=DEFAULT_PORT, log=None):
        self.port = port
        self.log = log or (lambda *a: None)
        self.winwork = WIN_WORKDIR
        self.wslwork = Path(win_to_wsl(WIN_WORKDIR))
        self.wslwork.mkdir(parents=True, exist_ok=True)

    def once(self, env, timeout):
        env = dict(env, port=self.port)
        name = "env-%s.json" % uuid.uuid4().hex
        (self.wslwork / name).write_text(json.dumps(env, ensure_ascii=True), encoding="utf-8")
        try:
            cp = subprocess.run([NODE_EXE, "-", self.winwork + "\\" + name], input=NODE_PROGRAM.encode("utf-8"),
                                capture_output=True, timeout=timeout)
            rc, so, se = cp.returncode, cp.stdout.decode("utf-8", "replace"), cp.stderr.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            return {"launch_failure": False, "timeout": True, "error": "NODE_TIMEOUT"}
        except OSError as e:
            return {"launch_failure": True, "error": "OSERROR %s" % e}
        finally:
            with contextlib.suppress(OSError):
                (self.wslwork / name).unlink()
        res = None
        for line in so.splitlines():
            if line.startswith("RELAY_RESULT:"):
                res = json.loads(line[len("RELAY_RESULT:"):])
        if res is None:
            return {"launch_failure": is_launch_failure(rc, so, se), "error": "NO_RESULT rc=%s" % rc, "stderr": se[-300:]}
        return {"launch_failure": False, "res": res}

    def run(self, phase, env, timeout=60):
        attempt = 0
        last = None
        while True:
            attempt += 1
            last = self.once(env, timeout)
            last["attempts"] = attempt
            if "res" in last and last["res"].get("ok"):
                return last
            if not should_retry(phase, attempt, last.get("launch_failure")):
                return last
            self.log("retry %s attempt=%d reason=%s" % (phase, attempt, last.get("error") or last["res"].get("error")))
            time.sleep(2)


def ok_result(r):
    return "res" in r and r["res"].get("ok")


def verify_owner(node):
    r = node.run("verify", {"op": "verify_owner"}, timeout=90)
    if not ok_result(r):
        return False, "VERIFY_FAILED: %s" % (r.get("error") or r.get("res"))
    ok, why = owner_ok(r["res"]["result"]["entries"])
    return ok, why


def list_targets(node):
    r = node.run("list", {"op": "list_targets"})
    if not ok_result(r):
        raise RuntimeError("LIST_FAILED %s" % (r.get("error") or r.get("res")))
    return r["res"]["result"]["targets"]


def find_chat_target(node, chat_url):
    ts = [t for t in list_targets(node) if t["url"] == chat_url]
    if len(ts) != 1:
        raise RuntimeError("TARGET_NOT_UNIQUE count=%d for %s" % (len(ts), chat_url))
    return ts[0]


def find_or_open_chat(node, chat_url, allow_open):
    """Return (target_id, created). Exactly one existing page with the exact URL, or (if allowed and none) open ONE new tab on it."""
    ts = [t for t in list_targets(node) if t["url"] == chat_url]
    if len(ts) == 1:
        return ts[0]["id"], False
    if len(ts) == 0 and allow_open:
        r = node.run("open_new", {"op": "open_new", "url": chat_url}, timeout=90)
        if not ok_result(r):
            raise RuntimeError("OPEN_CHAT_FAILED %s" % (r.get("error") or r.get("res")))
        return r["res"]["result"]["id"], True
    raise RuntimeError("TARGET_NOT_UNIQUE count=%d for %s" % (len(ts), chat_url))


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def cmd_verify_owner(a):
    node = Node(a.port)
    with lab_lock(a.lock):
        ok, why = verify_owner(node)
    emit({"owner_ok": ok, "why": why})
    return 0 if ok else 2


def login_status(node):
    r = node.run("login", {"op": "login_probe"}, timeout=90)
    if not ok_result(r):
        return None
    return bool(r["res"]["result"].get("logged_in"))


def cmd_login_status(a):
    node = Node(a.port)
    with lab_lock(a.lock):
        ok, why = verify_owner(node)
        if not ok:
            emit({"owner_ok": False, "why": why})
            return 2
        emit({"owner_ok": True, "logged_in": login_status(node)})
    return 0


def cmd_list_targets(a):
    node = Node(a.port)
    with lab_lock(a.lock):
        ok, why = verify_owner(node)
        if not ok:
            emit({"owner_ok": False, "why": why})
            return 2
        emit({"owner_ok": True, "targets": list_targets(node)})
    return 0


def cmd_send(a):
    node = Node(a.port, log=lambda m: print("[io]", m, file=sys.stderr))
    prompt = Path(a.prompt_file).read_text(encoding="utf-8")
    if prompt.endswith("\n"):
        prompt = prompt.rstrip("\n")  # composer readback has no trailing newline
    job = Path(a.job_dir)
    policy = json.loads((job / "job.json").read_text(encoding="utf-8"))
    ticket = a.dispatch_ticket
    pd.check(policy, job, a.instruction_id, phase="prepare",
             chat=policy.get("dispatch_chat_url"), ticket=ticket)
    rpath = job / ("receipt-%s.json" % a.instruction_id)
    if rpath.exists():
        old = Receipt.load(rpath)
        if old.stage in POST_INTENT:
            emit({"status": "REFUSED_ALREADY_ATTEMPTED", "stage": old.stage, "receipt": str(rpath),
                  "note": "no automatic resend; reconcile read-only (collect) first"})
            return 3
    rcpt = Receipt(rpath, a.instruction_id, hashlib.sha256(prompt.encode("utf-8")).hexdigest(), len(prompt))
    rcpt.update(mode="new" if a.new else "chat", chat_url=a.chat_url, dry_run=bool(a.dry_run))
    created = None
    with lab_lock(a.lock):
        ok, why = verify_owner(node)
        if not ok:
            rcpt.advance("PRESEND_FAILED", reason="OWNER:" + why)
            emit({"status": "PRESEND_FAILED", "reason": "OWNER:" + why, "receipt": str(rpath)})
            return 2
        try:
            if a.via_target:
                from urllib.parse import urlparse
                path = "/" if a.new else urlparse(a.chat_url).path
                r = node.run("spa_goto", {"op": "spa_goto", "target": a.via_target, "path": path}, timeout=90)
                if not ok_result(r) or not r["res"]["result"].get("ok"):
                    raise RuntimeError("SPA_GOTO_FAILED %s" % (r.get("error") or r.get("res")))
                target = a.via_target
                rcpt.update(target=target, via_target=True)
            elif a.new:
                r = node.run("open_new", {"op": "open_new", "url": "https://chatgpt.com/"}, timeout=60)
                if not ok_result(r):
                    raise RuntimeError("OPEN_NEW_FAILED %s" % (r.get("error") or r.get("res")))
                created = r["res"]["result"]["id"]
                target = created
                rcpt.update(target=target, created_target=True)
            else:
                target, created = find_or_open_chat(node, a.chat_url, a.open_if_missing)
                created = target if created else None
                rcpt.update(target=target, created_target=bool(created))
        except Exception as e:  # noqa
            rcpt.advance("PRESEND_FAILED", reason=str(e))
            emit({"status": "PRESEND_FAILED", "reason": str(e), "receipt": str(rpath)})
            return 2
        pd.check(policy, job, a.instruction_id, phase="prepare",
                 chat=policy.get("dispatch_chat_url"), ticket=ticket)
        env = {"target": target, "new": bool(a.new), "chat_url": a.chat_url, "prompt": prompt}
        r = node.run("prepare", dict(env, op="prepare"), timeout=90)
        if not ok_result(r) or not r["res"]["result"].get("ok") or not r["res"]["result"].get("draft_match"):
            why = r.get("error") or r.get("res")
            if created:  # our own empty tab, nothing sent: do not leave strays
                node.run("close", {"op": "close", "target": created}, timeout=30)
            rcpt.advance("PRESEND_FAILED", reason="PREPARE:%s" % json.dumps(why, ensure_ascii=False)[:300], target=target)
            emit({"status": "PRESEND_FAILED", "reason": "PREPARE", "detail": why, "receipt": str(rpath), "target": target})
            return 2
        prep = r["res"]["result"]
        baseline = count_matching(prompt, prep["bubbles"])
        rcpt.advance("DRAFT_VERIFIED", target=target, url=prep["url"], baseline_matches=baseline,
                     send=prep["send"], inserted=prep["inserted"])
        if a.dry_run:
            node.run("clear", dict(env, op="clear"), timeout=60)
            if created:
                node.run("close", {"op": "close", "target": created}, timeout=30)
            rcpt.advance("DRY_RUN_CLEARED", closed_created_target=bool(created))
            emit({"status": "DRY_RUN_OK", "send_buttons": prep["send"], "labels": prep["labels"], "receipt": str(rpath)})
            return 0
        if len(prep["send"]) != 1 or prep["send"][0]["disabled"]:
            rcpt.advance("PRESEND_FAILED", reason="SEND_BUTTON %s" % json.dumps(prep["send"]))
            emit({"status": "PRESEND_FAILED", "reason": "SEND_BUTTON", "send": prep["send"], "receipt": str(rpath)})
            return 2
        # ---- point of no return: intent is durable BEFORE the click
        pd.phase(policy, job, a.instruction_id, "SEND_INTENT",
                 chat=policy.get("dispatch_chat_url"), ticket=ticket)
        rcpt.advance("SEND_INTENT", target=target, url=prep["url"])
        pd.phase(policy, job, a.instruction_id, "SEND_UNKNOWN",
                 chat=policy.get("dispatch_chat_url"), ticket=ticket)
        r = node.run("click", dict(env, op="click"), timeout=45)
        clicked = ok_result(r) and r["res"]["result"].get("clicked")
        if not clicked:
            rcpt.advance("SEND_UNKNOWN", reason="CLICK_NOT_ACKED", detail=json.dumps(r.get("res") or r.get("error"), ensure_ascii=False)[:300],
                         launch_failure=bool(r.get("launch_failure")))
            emit({"status": "SEND_UNKNOWN", "reason": "CLICK_NOT_ACKED", "receipt": str(rpath), "target": target})
            return 4
        rcpt.advance("CLICK_ACK")
        deadline = time.monotonic() + a.confirm_timeout
        last = None
        while time.monotonic() < deadline:
            time.sleep(3)
            r = node.run("readback", {"op": "snapshot", "target": target}, timeout=45)
            if not ok_result(r):
                last = r.get("error") or r.get("res")
                continue
            s = r["res"]["result"]
            verdict = classify_readback(prompt, s["bubbles"], baseline)
            if verdict == "CONFIRMED":
                rcpt.advance("CONFIRMED", url=s["url"], target=target, evidence="EXACT_RENDERED_USER_BUBBLE")
                pd.phase(policy, job, a.instruction_id, "SENT_CONFIRMED",
                         chat=policy.get("dispatch_chat_url"), ticket=ticket)
                emit({"status": "CONFIRMED", "chat_url": s["url"], "target": target, "instruction_id": a.instruction_id,
                      "submit_count": rcpt.data["submit_count"], "receipt": str(rpath)})
                return 0
            last = verdict
        rcpt.advance("SEND_UNKNOWN", reason="READBACK_NOT_CONFIRMED:%s" % last)
        emit({"status": "SEND_UNKNOWN", "reason": "READBACK_NOT_CONFIRMED", "last": str(last), "receipt": str(rpath), "target": target})
        return 4


def write_atomic(path, text):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def cmd_collect(a):
    node = Node(a.port, log=lambda m: print("[io]", m, file=sys.stderr))
    deadline = time.monotonic() + a.timeout
    stab = Stability()
    reloaded = False
    result = {"chat_url": a.chat_url, "started": now_utc(), "reload_used": False}
    status = "TIMEOUT"
    answer = ""
    while time.monotonic() < deadline:
        with lab_lock(a.lock):
            ok, why = verify_owner(node)
            if not ok:
                status = "OWNER_CHECK_FAILED:" + why
                break
            try:
                tgt = find_chat_target(node, a.chat_url)["id"]
            except Exception as e:  # noqa
                status = "TARGET_NOT_FOUND:" + str(e)
                break
            r = node.run("snapshot", {"op": "snapshot", "target": tgt}, timeout=60)
            snap = r["res"]["result"] if ok_result(r) else None
            an = analyze_collect(snap, a.end_marker) if snap else None
            if an and an["status"] == "LOAD_FAILED" and a.reload and not reloaded:
                reloaded = True
                result["reload_used"] = True
                rr = node.run("reload", {"op": "reload", "target": tgt}, timeout=90)
                result["reload_result"] = rr.get("res") or rr.get("error")
                stab = Stability()
                continue
        if snap is None:
            result["last_error"] = r.get("error") or r.get("res")
            time.sleep(a.interval)
            continue
        answer = an["answer"]
        n = stab.feed(answer)
        result.update(last_status=an["status"], busy=an["busy"], answer_len=len(answer), stable=n, url=snap["url"], target=tgt)
        if an["status"] == "COMPLETE_END" and n >= 2:
            status = "COMPLETE_END"
            break
        if an["status"] == "LOAD_FAILED" and n >= 2:
            status = "REPLY_LOAD_FAILED"
            break
        if an["status"] == "IDLE" and n >= 2 and (a.until_handback or not a.end_marker):
            status = "IDLE_HANDBACK" if a.until_handback else "IDLE_STABLE"
            break
        time.sleep(a.interval)
    result.update(status=status, finished=now_utc(), answer_sha256=hashlib.sha256(answer.encode("utf-8")).hexdigest())
    if a.out_answer and status in ("COMPLETE_END", "IDLE_HANDBACK", "IDLE_STABLE") and answer:
        write_atomic(a.out_answer, answer)
        result["answer_file"] = a.out_answer
    elif a.out_answer and answer:
        write_atomic(a.out_answer + ".partial", answer)
        result["partial_file"] = a.out_answer + ".partial"
    if a.out_json:
        write_atomic(a.out_json, json.dumps(result, ensure_ascii=False, indent=2))
    emit(result)
    return 0 if status in ("COMPLETE_END", "IDLE_HANDBACK", "IDLE_STABLE") else 5


def cmd_approve_card(a):
    node = Node(a.port)
    with lab_lock(a.lock):
        ok, why = verify_owner(node)
        if not ok:
            emit({"owner_ok": False, "why": why})
            return 2
        tgt = find_chat_target(node, a.chat_url)
        r = node.run("cards", {"op": "cards", "target": tgt["id"]})
        emit({"mode": "LIST_ONLY_NEVER_CLICKS", "target": tgt["id"], "url": tgt["url"],
              "cards": r["res"]["result"]["cards"] if ok_result(r) else r.get("error")})
    return 0


def cmd_status(a):
    """Read-only one-shot snapshot summary of an exact chat."""
    node = Node(a.port)
    with lab_lock(a.lock):
        ok, why = verify_owner(node)
        if not ok:
            emit({"owner_ok": False, "why": why})
            return 2
        tid, _ = find_or_open_chat(node, a.chat_url, getattr(a, "open_if_missing", False))
        tgt = {"id": tid}
        r = node.run("snapshot", {"op": "snapshot", "target": tgt["id"]}, timeout=60)
        if not ok_result(r):
            emit({"error": r.get("error") or r.get("res")})
            return 5
        s = r["res"]["result"]
        an = analyze_collect(s, a.end_marker)
        emit({"target": tgt["id"], "url": s["url"], "busy": s["busy"], "composer_len": len(s["composer"] or ""),
              "user_bubbles": [normalize_bubble(b.get("pre") or b.get("full"))[:int(a.head)] for b in s["bubbles"]],
              "analysis": an["status"], "answer_tail": an["answer"][-int(a.tail):], "send_buttons": s["send"],
              "text_tail": s["text"][-int(a.tail):]})
    return 0


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--lock", default=str(hermes_home() / "cache" / "auto_allow.lock"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verify-owner").set_defaults(fn=cmd_verify_owner)
    sub.add_parser("list-targets").set_defaults(fn=cmd_list_targets)
    sub.add_parser("login-status", help="boolean only; no tokens").set_defaults(fn=cmd_login_status)
    s = sub.add_parser("send")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--chat-url")
    g.add_argument("--new", action="store_true")
    s.add_argument("--job-dir", required=True)
    s.add_argument("--dispatch-ticket")
    s.add_argument("--instruction-id", required=True)
    s.add_argument("--prompt-file", required=True)
    s.add_argument("--dry-run", action="store_true", help="draft + verify + clear; never clicks send")
    s.add_argument("--no-open-if-missing", dest="open_if_missing", action="store_false",
                   help="by default a missing exact chat tab is reopened as ONE new tab on that URL")
    s.add_argument("--confirm-timeout", type=int, default=90)
    s.add_argument("--via-target", help="navigate INSIDE this already hydrated tab (SPA click) instead of opening a new tab")
    s.set_defaults(fn=cmd_send)
    c = sub.add_parser("collect")
    c.add_argument("--chat-url", required=True)
    c.add_argument("--end-marker")
    c.add_argument("--until-handback", action="store_true")
    c.add_argument("--reload", action="store_true", help="reload the page once on REPLY_LOAD_FAILED (never regenerates)")
    c.add_argument("--timeout", type=int, default=600)
    c.add_argument("--interval", type=int, default=8)
    c.add_argument("--out-answer")
    c.add_argument("--out-json")
    c.set_defaults(fn=cmd_collect)
    st = sub.add_parser("status", help="read-only snapshot summary")
    st.add_argument("--chat-url", required=True)
    st.add_argument("--end-marker")
    st.add_argument("--open-if-missing", action="store_true")
    st.add_argument("--head", default=160)
    st.add_argument("--tail", default=1500)
    st.set_defaults(fn=cmd_status)
    ap = sub.add_parser("approve-card")
    ap.add_argument("--chat-url", required=True)
    ap.set_defaults(fn=cmd_approve_card)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except TimeoutError as e:
        emit({"status": "LOCK_TIMEOUT", "error": str(e)})
        return 6


if __name__ == "__main__":
    sys.exit(main())
