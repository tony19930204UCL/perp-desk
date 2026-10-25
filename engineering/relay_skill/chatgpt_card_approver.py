#!/usr/bin/env python3
"""Scoped single-use approver for ChatGPT 'allow GitHub?' cards of ONE job (user-authorized 10/25).

details: expand the card's detail view (read-only UI toggle) and print the card text.
allow:   click 'Allow once' for exactly one card, only if ALL policy checks pass on the expanded text:
         card title present, exactly one allow button, a job write_allowlist path (or the job repo+branch)
         named, no secret-like content, no other repo, no main-branch/merge/delete wording.
Never clicks 'always'/'remember', never edits the composer, never sends, never retries a click.
Shares cache/auto_allow.lock with the other browser tools. Writes evidence to <job>/card-approvals/.
"""
import argparse, json, re, sys, time
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
SK = Path(__file__).resolve().parent
sys.path.insert(0, str(SK))
import chatgpt_page_io as io  # noqa: E402
from relay_fixes import card_guard, RelayFixError
import product_dispatch as pd

TITLE = "要允許 ChatGPT 使用 GitHub 嗎？"
SECRET = re.compile(r"(?i)(sk-[a-z0-9]{20,}|gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,}|PRIVATE KEY|api[_-]?key\s*[=:]\s*[\"'][a-z0-9]{20,}|\.env\b)")
BAD = re.compile(r"(?i)(merge|刪除|delete|force|main 分支|到 main|settings|secrets?|workflow)")

EXTRA = r"""
  async card_text() {
    return await withTarget(ENV.target, async (p) => {
      return await evalIn(p, function (arg) {
        const root = Array.from(document.querySelectorAll('*')).filter(e => e.children.length > 0 && /要允許 ChatGPT 使用 GitHub 嗎/.test(e.innerText || '') && (e.innerText || '').length < 6000);
        const card = root.sort((a, b) => a.innerText.length - b.innerText.length)[0];
        if (!card) return { ok: false, why: 'NO_CARD' };
        if (arg.expand) {
          const d = Array.from(card.querySelectorAll('a,button,span,div')).find(e => /^查看詳細資訊$/.test((e.innerText || '').trim()));
          if (d) d.click();
        }
        return { ok: true, text: card.innerText };
      }, { expand: !!ENV.expand }, 15000);
    }, 'always');
  },
  async card_allow() {
    return await withTarget(ENV.target, async (p) => {
      return await evalIn(p, function () {
        const root = Array.from(document.querySelectorAll('*')).filter(e => e.children.length > 0 && /要允許 ChatGPT 使用 GitHub 嗎/.test(e.innerText || '') && (e.innerText || '').length < 6000);
        const card = root.sort((a, b) => a.innerText.length - b.innerText.length)[0];
        if (!card) return { ok: false, why: 'NO_CARD' };
        const btns = Array.from(card.querySelectorAll('button')).filter(b => /^允許一次(\s|$)/.test((b.innerText || '').trim()) && b.getClientRects().length);
        if (btns.length !== 1) return { ok: false, why: 'ALLOW_BUTTONS_' + btns.length };
        btns[0].click();
        return { ok: true, clicked: true };
      }, null, 15000);
    }, 'always');
  },
  async close() {"""

PROGRAM = io.NODE_PROGRAM.replace("  async close() {", EXTRA.lstrip("\n"), 1)
assert PROGRAM != io.NODE_PROGRAM


def run_op(node, op, **kw):
    orig = io.NODE_PROGRAM
    io.NODE_PROGRAM = PROGRAM
    try:
        r = node.run(op, dict({"op": op}, **kw), timeout=60)
    finally:
        io.NODE_PROGRAM = orig
    return r.get("res", r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["details", "allow"])
    ap.add_argument("--job", required=True)
    a = ap.parse_args()
    job = json.loads((Path(a.job) / "job.json").read_text())
    chat = job["chat_url"]
    allow = job["write_allowlist"]
    out = Path(a.job) / "card-approvals"
    out.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    with io.lab_lock(str(HOME / "cache" / "auto_allow.lock")):
        node = io.Node()
        tgt = io.find_chat_target(node, chat)
        if not tgt:
            print(json.dumps({"status": "NO_TARGET"})); return 3
        tid = tgt["id"] if isinstance(tgt, dict) else tgt
        r = run_op(node, "card_text", target=tid, expand=True)
        time.sleep(1.5)
        r = run_op(node, "card_text", target=tid, expand=False)
        text = (r.get("result") or {}).get("text", "")
        (out / ("card-%s.txt" % stamp)).write_text(text)
        if a.mode == "details":
            print(text); return 0
        try:
            card_sha = card_guard(job,text)
            verdict = True
            checks = {"scoped_single_use":True}
        except RelayFixError as exc:
            card_sha = __import__("hashlib").sha256(text.encode()).hexdigest()
            verdict = False
            checks = {"scoped_single_use":False,"reason":str(exc)}
        ev = {"at": stamp, "checks": checks, "decision": "CLICK" if verdict else "REFUSE", "card_sha": __import__("hashlib").sha256(text.encode()).hexdigest()}
        if verdict:
            iid = json.loads((Path(a.job)/"active_instruction.json").read_text())["instruction_id"]
            pd.approval_once(job, Path(a.job), iid, card_sha, chat=chat)
            pd.approve_check(job, Path(a.job), iid, card_sha, chat=chat)
            c = run_op(node, "card_allow", target=tid)
            ev["click"] = c
            if not (c.get("result") or {}).get("clicked"):
                ev["status"] = "CARD_UNKNOWN"
                print(json.dumps(ev, ensure_ascii=False)); return 5
            time.sleep(4)
            ev["after"] = (run_op(node, "card_text", target=tid, expand=False).get("result") or {}).get("ok")
            if ev["after"] is not False:
                ev["status"] = "CARD_UNKNOWN"
                print(json.dumps(ev, ensure_ascii=False)); return 5
            pd.reconcile(job, Path(a.job), iid, receipt="APPROVED",
                         chat=chat, fingerprint=card_sha)
        (out / ("decision-%s.json" % stamp)).write_text(json.dumps(ev, ensure_ascii=False, indent=1))
        print(json.dumps(ev, ensure_ascii=False))
        return 0 if verdict and ev.get("after") is False else 4


if __name__ == "__main__":
    sys.exit(main())
