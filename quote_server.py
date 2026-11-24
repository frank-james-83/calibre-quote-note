#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quote_server.py —— 本地摘录服务（带 AI 简短回应），Python 标准库版
在 calibre 阅读器右侧「查找」面板里加载录入表单；写下想法后可以让 DeepSeek 回一句，
一起写进 Markdown 笔记。只监听 127.0.0.1，不对外网开放。

DeepSeek Key 的读取顺序：
  1) 环境变量 DEEPSEEK_API_KEY
  2) 本目录 quote_settings.json 里的 deepseek_api_key
  3) %USERPROFILE%\\.dsh\\deepseek_api_key.txt（纯文本一行）

用法：
  python quote_server.py
  python quote_server.py --test
"""

import argparse
import html
import json
import os
import socket
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------- 配置与路径 ----------------

ROOT = Path(__file__).resolve().parent
SETTINGS_PATH = ROOT / "quote_settings.json"

DEFAULT_SETTINGS = {
    "note_path": "",
    "last_book": "",
    "last_loc": "",
    "ai_base_url": "https://api.deepseek.com",
    "ai_model": "deepseek-chat",
    "ai_max_chars": 50,
    "ai_timeout": 45,
    "deepseek_api_key": "",
}


def default_note_path() -> str:
    home = Path.home()
    docs = home / "Documents"
    if not docs.exists():
        docs = home
    return str(docs / "摘录笔记.md")


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    s["note_path"] = default_note_path()
    if SETTINGS_PATH.exists():
        try:
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            for k in s:
                if k in raw and raw[k] not in (None, ""):
                    if k in ("ai_max_chars", "ai_timeout"):
                        s[k] = int(raw[k])
                    else:
                        s[k] = str(raw[k])
            # 兼容旧字段 last_source
            if not raw.get("last_book") and raw.get("last_source"):
                parts = str(raw["last_source"]).split(" · ", 1)
                s["last_book"] = parts[0]
                if len(parts) > 1:
                    s["last_loc"] = parts[1]
        except Exception:
            pass
    return s


def save_settings(s: dict) -> None:
    try:
        obj = {k: s.get(k, DEFAULT_SETTINGS[k]) for k in DEFAULT_SETTINGS}
        SETTINGS_PATH.write_text(
            json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def get_api_key(s: dict) -> str:
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    if s.get("deepseek_api_key"):
        return s["deepseek_api_key"]
    alt = Path.home() / ".dsh" / "deepseek_api_key.txt"
    if alt.exists():
        try:
            return alt.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""


def known_books(note_path: str, limit: int = 15) -> list:
    books = []
    p = Path(note_path)
    if p.exists():
        try:
            with p.open(encoding="utf-8") as f:
                for line in f:
                    if line.startswith("**出处**："):
                        book = line[len("**出处**："):].strip().split(" · ", 1)[0].strip()
                        if book and book not in books:
                            books.append(book)
        except Exception:
            pass
    return books[-limit:]


# ---------------- 文本与 Markdown ----------------

def format_source(book: str, loc: str, fallback: str = "") -> str:
    b, l = book.strip(), loc.strip()
    if b and l:
        return f"{b} · {l}"
    if b:
        return b
    return fallback.strip()


def format_entry(
    quote: str,
    source: str,
    thoughts: str,
    ai_reply: str,
    ai_max: int,
    thread: list | None = None,
) -> str:
    lines = ["---", "", f"## {datetime.now():%Y-%m-%d %H:%M}", "", "**原文**", ""]
    for line in quote.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        lines.append("> " + line)
    lines += ["", f"**出处**：{source}", "", "**想法**", "", thoughts]
    items = get_thread_items(thread)
    if items and items[0].get("r") == "ai":
        lines += ["", f"**AI 回应**（{ai_max} 字以内）", "", str(items[0].get("t", "")).strip()]
        round_no = 0
        for i in range(1, len(items)):
            item = items[i]
            if item.get("r") == "user":
                round_no += 1
                lines += ["", f"**还嘴 {round_no}**"]
            else:
                lines += ["", "**AI 再回**"]
            lines += ["", str(item.get("t", "")).strip()]
    elif ai_reply and ai_reply.strip():
        lines += ["", f"**AI 回应**（{ai_max} 字以内）", "", ai_reply.strip()]
    lines.append("")
    return "\n".join(lines) + "\n"


def add_entry(path: str, text: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text("# 摘录笔记\n", encoding="utf-8")
    with p.open("a", encoding="utf-8") as f:
        f.write(text)
    return str(p.resolve())


# ---------------- AI 调用 ----------------

def new_ai_system(max_chars: int, debate: bool = False) -> str:
    n = max(10, min(400, max_chars))
    if debate:
        return (
            "你是一位尊重读者、认真求真的读书同伴，正在围绕摘录交流。"
            "回应读者时，先理解并承接其反驳中成立或有价值的部分；若它修正了你之前的判断，坦率承认。"
            "只有在分歧会实质影响结论时才指出，并说明具体原因；不要抓住措辞瑕疵猛攻，也不要为了反驳而反驳。"
            "若双方各有道理，清楚说明共识与分歧，把讨论推进一步；不必强行分胜负。"
            "不空泛奉承，不重复旧话，不分点。"
            f"用中文，控制在 {n} 个字以内，语气坦诚、友善。"
        )
    return (
        "你是一位尊重读者、认真求真的读书同伴。用户分享了摘录和自己的想法。"
        "先理解其核心，并具体回应其中成立、有洞见或值得保留的部分；不要泛泛夸赞。"
        "只有当事实或推理问题会实质影响结论时才指出，并给出更准确的理解；轻微措辞不严谨不必追打。"
        "若核心想法成立，可补充一个有帮助的新角度；不要为了显得深刻而强行反驳。"
        "不复述原文，不客套，不分点。"
        f"用中文，控制在 {n} 个字以内，语气坦诚、友善。"
    )


def invoke_ai_messages(s: dict, system: str, messages: list, max_chars: int) -> str:
    key = get_api_key(s)
    if not key:
        raise RuntimeError("还没有配置 DeepSeek API Key：打开 /setup 填一下，或改 quote_settings.json 的 deepseek_api_key 后重启服务")
    n = max(10, min(400, max_chars))
    body = {
        "model": s.get("ai_model", "deepseek-chat"),
        "messages": [
            {"role": "system", "content": system},
        ] + messages,
        "temperature": 0.7,
        "max_tokens": 400,
        "stream": False,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    uri = s.get("ai_base_url", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        uri,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=int(s.get("ai_timeout", 45))) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}：{detail or e.reason}")
    except Exception as e:
        raise RuntimeError(str(e))
    text = (payload.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    text = text.strip()
    if not text:
        raise RuntimeError("AI 返回了空内容")
    if len(text) > n + 10:
        text = text[:n] + "…"
    return text


def invoke_ai(s: dict, quote: str, source: str, thoughts: str, max_chars: int) -> str:
    system = new_ai_system(max_chars, debate=False)
    user = f"出处：{source}\n原文：{quote}\n我的想法：{thoughts}"
    return invoke_ai_messages(s, system, [{"role": "user", "content": user}], max_chars)


def invoke_ai_debate(
    s: dict,
    quote: str,
    source: str,
    thoughts: str,
    thread: list,
    max_chars: int,
) -> str:
    system = new_ai_system(max_chars, debate=True)
    messages = [{"role": "user", "content": f"出处：{source}\n原文：{quote}\n我的想法：{thoughts}"}]
    for item in get_thread_items(thread):
        role = "assistant" if item.get("r") == "ai" else "user"
        messages.append({"role": role, "content": str(item.get("t", ""))})
    return invoke_ai_messages(s, system, messages, max_chars)


# ---------------- HTML 模板 ----------------

CSS = """
*{box-sizing:border-box}
body{margin:0;padding:10px 12px 18px;font:13px/1.5 "Segoe UI","Microsoft YaHei",sans-serif;color:#222}
h1{font-size:14px;margin:0 0 10px;font-weight:600}
label{display:block;font-size:12px;color:#555;margin:10px 0 3px}
textarea,input,select{width:100%;font:inherit;color:inherit;border:1px solid #c8c8c8;border-radius:4px;padding:6px;background:#fff}
textarea{resize:vertical}
.q{height:140px}.t{height:96px}
.two{display:flex;gap:8px}
.two > div{flex:1}
.row{display:flex;gap:8px;margin-top:14px}
.row.tight{margin-top:10px}
button{font:inherit;padding:8px 10px;border-radius:4px;border:1px solid #c8c8c8;background:#f6f6f6;cursor:pointer}
button.primary{background:#2a6fdb;border-color:#2a6fdb;color:#fff;flex:1.4}
button.ai{background:#8a5cf5;border-color:#8a5cf5;color:#fff;flex:1}
button.ghost{flex:0 0 auto}
.ai-block{margin-top:12px;border:1px solid #d8cffa;border-radius:6px;padding:8px 10px;background:#faf8ff}
.ai-block .hint{font-size:11px;color:#7a6bb5;margin-bottom:4px}
.ai-text{font-size:13px;white-space:pre-wrap}
.debate{margin-top:12px;border:1px solid #d8cffa;border-radius:6px;padding:8px 10px;background:#faf8ff}
.debate .hint{font-size:11px;color:#7a6bb5;margin-bottom:6px}
.say{margin:6px 0;padding:6px 8px;border-radius:5px;font-size:12.5px;white-space:pre-wrap}
.say .who{display:inline-block;font-size:11px;font-weight:600;margin-right:6px;opacity:.75}
.say.ai{background:#efe8ff;color:#2f2350}
.say.me{background:#eef3ff;color:#1d2f52}
.warn{margin-top:10px;font-size:12px;color:#a35a00;background:#fff6e6;border:1px solid #f0d9ae;border-radius:4px;padding:6px 8px}
.path{margin-top:10px;font-size:11px;color:#888;word-break:break-all}
.ok{font-size:14px;padding:14px 0}
.muted{color:#888;font-size:12px}
.chk{display:flex;align-items:center;gap:6px;margin-top:8px;font-size:12px;color:#444}
.chk input{width:auto}
"""

FORM_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>摘录到笔记</title><style>__CSS__</style></head><body>
<h1>摘录到笔记</h1>
<form method="post" action="/save">
  <label for="quote">原文</label>
  <textarea id="quote" name="quote" class="q">__QUOTE__</textarea>
  <div class="two">
    <div>
      <label for="book">书名</label>
      <input id="book" name="book" list="books" value="__BOOK__" placeholder="文学回忆录 上">
      <datalist id="books">__BOOKS__</datalist>
    </div>
    <div>
      <label for="loc">位置</label>
      <input id="loc" name="loc" value="__LOC__" placeholder="p.23 / 第三讲">
    </div>
  </div>
  <label for="thoughts">想法</label>
  <textarea id="thoughts" name="thoughts" class="t" placeholder="当时想到什么…">__THOUGHTS__</textarea>
  <div class="row tight">
    <button type="submit" formaction="/save" class="primary">保存并追加</button>
    <button type="submit" formaction="/discuss" class="ai">让 AI 回一句</button>
  </div>
  <div class="row tight">
    <div style="flex:0 0 90px">
      <label for="max_chars">字数上限</label>
      <input id="max_chars" name="max_chars" value="__MAXCHARS__">
    </div>
    <div style="flex:0 0 auto;align-self:flex-end">
      <button type="button" class="ghost" onclick="document.getElementById('thoughts').value=''">清空想法</button>
    </div>
  </div>
  __AIBLOCK__
  __THREAD__
  __REBUT__
  __WARN__
</form>
<div class="path">写入：__PATH__</div>
<script>
document.getElementById('thoughts').addEventListener('keydown', function(e){
  if (e.key==='Enter' && (e.ctrlKey||e.metaKey)) { e.preventDefault(); this.form.submit(); }
});
</script>
</body></html>
"""

SAVED_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>已保存</title><style>__CSS__</style></head><body>
<h1>已保存</h1>
<div class="ok">这条摘录已经追加到笔记文件。</div>
__AIBLOCK__
<div class="path">__PATH__</div>
<div class="muted" style="margin-top:14px">选中下一段文字，右侧面板会重新载入录入表单。</div>
</body></html>
"""

ERROR_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>出错了</title><style>__CSS__</style></head><body>
<h1>出错了</h1>
<pre style="white-space:pre-wrap;font-size:12px">__ERROR__</pre>
<div class="muted" style="margin-top:12px">回到上一页可继续编辑。</div>
</body></html>
"""

SETUP_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>摘录服务设置</title><style>__CSS__</style></head><body>
<h1>摘录服务设置</h1>
__SAVED__
<form method="post" action="/setup">
  <label for="key">DeepSeek API Key</label>
  <input id="key" name="deepseek_api_key" value="__KEY__" placeholder="sk-...">
  <label for="note">笔记文件</label>
  <input id="note" name="note_path" value="__NOTE__">
  <label for="chars">AI 回应字数上限</label>
  <input id="chars" name="ai_max_chars" value="__CHARS__">
  <div class="row">
    <button type="submit" class="primary">保存设置</button>
    <button type="button" onclick="location.href='/quote'">回到录入表单</button>
  </div>
</form>
<div class="path">Key 只存在本机的 quote_settings.json 里，不会上传到别处（除了你自己配置的 API 端点）。</div>
</body></html>
"""


def render(tpl: str, mapping: dict) -> str:
    out = tpl.replace("__CSS__", CSS)
    for k, v in mapping.items():
        out = out.replace(k, str(v))
    return out


def build_ai_block(reply: str, max_chars: int) -> str:
    if not reply or not reply.strip():
        return ""
    r = html.escape(reply.strip())
    return (
        '<div class="ai-block">\n'
        f'<div class="hint">AI 回应 · 上限 {max_chars} 字（可直接改，也可取消勾选不写入）</div>\n'
        f'<div class="ai-text">{r}</div>\n'
        '<label class="chk"><input type="checkbox" name="include_ai" value="1" checked> 把这段 AI 回应一起写进笔记</label>\n'
        f'<input type="hidden" name="ai_reply" value="{r}">\n'
        "</div>"
    )


def get_thread_items(thread) -> list:
    if not isinstance(thread, list):
        return []
    return [item for item in thread if isinstance(item, dict) and item.get("r") in ("ai", "user")]


def convert_to_thread_json(thread: list) -> str:
    items = get_thread_items(thread)
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def convert_from_thread_json(text: str) -> list:
    if not text:
        return []
    try:
        return get_thread_items(json.loads(text))
    except Exception:
        return []


def build_debate_html(thread: list, with_form: bool = True) -> str:
    items = get_thread_items(thread)
    if not items:
        return ""
    parts = ['<div class="debate"><div class="hint">和 AI 的往复</div>']
    for item in items:
        is_ai = item.get("r") == "ai"
        label = "AI" if is_ai else "我"
        cls = "ai" if is_ai else "me"
        text = html.escape(str(item.get("t", "")))
        parts.append(
            f'<div class="say {cls}"><span class="who">{label}</span><span class="txt">{text}</span></div>'
        )
    parts.append("</div>")
    if with_form:
        thread_json = html.escape(convert_to_thread_json(items), quote=True)
        parts.append(
            f'<input type="hidden" name="thread" value="{thread_json}">'
        )
        parts.append(
            '<label class="chk"><input type="checkbox" name="include_ai" value="1" checked> '
            "把这段往复一起写进笔记</label>"
        )
    return "".join(parts)


def build_rebuttal_block(thread: list) -> str:
    if not get_thread_items(thread):
        return ""
    return (
        '<label for="rebut">还嘴</label>'
        '<textarea id="rebut" name="rebut" class="t" '
        'placeholder="不同意就直接说：这个判断把哪两件事混为一谈了"></textarea>'
        '<div class="row tight"><button type="submit" formaction="/rebut" class="ai">再辩一轮</button></div>'
    )


# ---------------- HTTP 处理器 ----------------

class QuoteHandler(BaseHTTPRequestHandler):
    server_version = "QuoteServer/1.0"

    def log_message(self, fmt, *args):
        try:
            log_path = Path(tempfile.gettempdir()) / "quote_server.log"
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%H:%M:%S}] {fmt % args}\n")
        except Exception:
            pass

    def _send_html(self, body: str, status: int = 200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _read_form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = raw.decode("utf-8", errors="replace")
        except Exception:
            body = ""
        parsed = urllib.parse.parse_qs(body, keep_blank_values=True)
        return {k: v[0] if v else "" for k, v in parsed.items()}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        s = load_settings()

        if path == "/setup":
            saved = ""
            self._send_html(render(SETUP_PAGE, {
                "__SAVED__": saved,
                "__KEY__": html.escape(s.get("deepseek_api_key", "")),
                "__NOTE__": html.escape(s.get("note_path", "")),
                "__CHARS__": html.escape(str(s.get("ai_max_chars", 50))),
            }))
            return

        # 默认 / 或 /quote
        text = ""
        if "text" in query:
            text = query["text"][0]
        elif "word" in query:
            text = query["word"][0]
        book = query.get("book", [s.get("last_book", "")])[0]
        loc = query.get("loc", [s.get("last_loc", "")])[0]
        opts = "".join(
            f'<option value="{html.escape(b)}">' for b in known_books(s.get("note_path", ""))
        )
        warn = ""
        if not get_api_key(s):
            warn = '<div class="warn">AI 回应还没启用：<a href="/setup">点这里填 DeepSeek API Key</a>，填完刷新本页。</div>'
        self._send_html(render(FORM_PAGE, {
            "__QUOTE__": html.escape(text),
            "__BOOK__": html.escape(book),
            "__LOC__": html.escape(loc),
            "__THOUGHTS__": "",
            "__MAXCHARS__": html.escape(str(s.get("ai_max_chars", 50))),
            "__BOOKS__": opts,
            "__AIBLOCK__": "",
            "__THREAD__": "",
            "__REBUT__": "",
            "__WARN__": warn,
            "__PATH__": html.escape(s.get("note_path", "")),
        }))

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        f = self._read_form()
        s = load_settings()

        quote = f.get("quote", "")
        book = f.get("book", "")
        loc = f.get("loc", "")
        thoughts = f.get("thoughts", "")
        max_chars = int(s.get("ai_max_chars", 50))
        try:
            if f.get("max_chars"):
                tmp = int(f["max_chars"])
                if tmp > 0:
                    max_chars = tmp
        except Exception:
            pass
        ai_reply = f.get("ai_reply", "")
        thread = convert_from_thread_json(f.get("thread", ""))
        rebut = f.get("rebut", "")

        if path == "/save":
            if not f.get("include_ai"):
                ai_reply = ""
                thread = []
            if not quote.strip():
                self._send_html(render(ERROR_PAGE, {
                    "__ERROR__": html.escape("原文是空的，没有写入。")
                }))
                return
            try:
                source = format_source(book, loc, "")
                entry = format_entry(quote, source, thoughts, ai_reply, max_chars, thread)
                written = add_entry(s.get("note_path", default_note_path()), entry)
                s["last_book"] = book
                s["last_loc"] = loc
                s["ai_max_chars"] = max_chars
                save_settings(s)
                shown = build_ai_block(ai_reply, max_chars)
                if get_thread_items(thread):
                    shown = build_debate_html(thread, with_form=False)
                self._send_html(render(SAVED_PAGE, {
                    "__PATH__": html.escape(written),
                    "__AIBLOCK__": shown,
                }))
            except Exception as e:
                self._send_html(render(ERROR_PAGE, {"__ERROR__": html.escape(str(e))}))
            return

        if path == "/discuss":
            ai_reply = ""
            warn = ""
            thread = []
            source = format_source(book, loc, "")
            if not thoughts.strip():
                warn = '<div class="warn">先写一句想法，AI 才有东西可回。</div>'
            elif not get_api_key(s):
                warn = '<div class="warn">还没有配置 DeepSeek API Key：<a href="/setup">点这里填</a>（或改 quote_settings.json 的 deepseek_api_key 后重启服务）。</div>'
            else:
                try:
                    ai_reply = invoke_ai(s, quote, source, thoughts, max_chars)
                    thread = [{"r": "ai", "t": ai_reply}]
                except Exception as e:
                    warn = '<div class="warn">AI 调用失败：' + html.escape(str(e)) + '</div>'
            opts = "".join(
                f'<option value="{html.escape(b)}">' for b in known_books(s.get("note_path", ""))
            )
            self._send_html(render(FORM_PAGE, {
                "__QUOTE__": html.escape(quote),
                "__BOOK__": html.escape(book),
                "__LOC__": html.escape(loc),
                "__THOUGHTS__": html.escape(thoughts),
                "__MAXCHARS__": html.escape(str(max_chars)),
                "__BOOKS__": opts,
                "__AIBLOCK__": "",
                "__THREAD__": build_debate_html(thread),
                "__REBUT__": build_rebuttal_block(thread),
                "__WARN__": warn,
                "__PATH__": html.escape(s.get("note_path", "")),
            }))
            return

        if path == "/rebut":
            warn = ""
            if not rebut.strip():
                warn = '<div class="warn">先把你的反驳写进「还嘴」，再辩一轮。</div>'
            elif not get_thread_items(thread):
                warn = '<div class="warn">还没有 AI 回应，先点一次「让 AI 回一句」。</div>'
            elif not get_api_key(s):
                warn = '<div class="warn">还没有配置 DeepSeek API Key：<a href="/setup">点这里填</a>。</div>'
            else:
                source = format_source(book, loc, "")
                try:
                    thread = get_thread_items(thread) + [{"r": "user", "t": rebut.strip()}]
                    reply = invoke_ai_debate(s, quote, source, thoughts, thread, max_chars)
                    thread = get_thread_items(thread) + [{"r": "ai", "t": reply}]
                except Exception as e:
                    warn = '<div class="warn">AI 调用失败：' + html.escape(str(e)) + '</div>'
            opts = "".join(
                f'<option value="{html.escape(b)}">' for b in known_books(s.get("note_path", ""))
            )
            self._send_html(render(FORM_PAGE, {
                "__QUOTE__": html.escape(quote),
                "__BOOK__": html.escape(book),
                "__LOC__": html.escape(loc),
                "__THOUGHTS__": html.escape(thoughts),
                "__MAXCHARS__": html.escape(str(max_chars)),
                "__BOOKS__": opts,
                "__AIBLOCK__": "",
                "__THREAD__": build_debate_html(thread),
                "__REBUT__": build_rebuttal_block(thread),
                "__WARN__": warn,
                "__PATH__": html.escape(s.get("note_path", "")),
            }))
            return

        if path == "/setup":
            if "deepseek_api_key" in f:
                s["deepseek_api_key"] = f["deepseek_api_key"].strip()
            if f.get("note_path", "").strip():
                s["note_path"] = f["note_path"].strip()
            if f.get("ai_max_chars"):
                try:
                    tmp = int(f["ai_max_chars"])
                    if tmp > 0:
                        s["ai_max_chars"] = tmp
                except Exception:
                    pass
            save_settings(s)
            saved = '<div class="warn" style="background:#eaf7ee;border-color:#bfe3c8;color:#1d6b32">已保存。</div>'
            self._send_html(render(SETUP_PAGE, {
                "__SAVED__": saved,
                "__KEY__": html.escape(s.get("deepseek_api_key", "")),
                "__NOTE__": html.escape(s.get("note_path", "")),
                "__CHARS__": html.escape(str(s.get("ai_max_chars", 50))),
            }))
            return

        self._send_html(render(ERROR_PAGE, {"__ERROR__": html.escape("未知路径")}), status=404)


# ---------------- 自检与启动 ----------------

def run_self_test() -> None:
    s = load_settings()
    entry = format_entry("第一行原文", "《测试书》 · p.1", "测试想法", "这句是 AI 的回应。", 50)
    tmp = Path(tempfile.gettempdir()) / f"quote_selftest_{os.urandom(4).hex()}.md"
    written = add_entry(str(tmp), entry)
    add_entry(written, format_entry("第二条", "书 · p.9", "想法二", "", 50))
    content = Path(written).read_text(encoding="utf-8")
    print(f"TEST_OK path={written} len={len(content)}")
    print("HAS_AI_SECTION=" + str("**AI 回应**（50 字以内）" in content))
    print("SECOND_HAS_AI=" + str("想法二\n\n---" in content))
    print("KEY_PRESENT=" + str(bool(get_api_key(s))))
    print("SOURCE=" + format_source("文学回忆录 上", "p.23", ""))
    thread = [
        {"r": "ai", "t": "这判断把「知识」和「信息的数量」混为一谈了。"},
        {"r": "user", "t": "我看重的是关系，不是数量。"},
        {"r": "ai", "t": "那也要说清关系是哪种，否则仍是比喻。"},
    ]
    json_text = convert_to_thread_json(thread)
    back = convert_from_thread_json(json_text)
    print("THREAD_JSON_LEN=" + str(len(json_text)) + " ROUNDTRIP=" + str(len(back)) + " FIRST=" + str(back[0]["r"] if back else ""))
    debate_entry = format_entry("原文", "书 · p.2", "想法", "", 50, back)
    print("DEBATE_HAS_ROUNDS=" + str("**还嘴 1**" in debate_entry and "**AI 再回**" in debate_entry))
    print("DEBATE_HTML_HAS_INPUT=" + str('name="thread"' in build_debate_html(back)))
    print("REBUT_BLOCK_SHOWN=" + str(bool(build_rebuttal_block(back))))
    print("--- note preview ---")
    print(content)
    try:
        Path(tmp).unlink()
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--note", default="")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    if args.test:
        run_self_test()
        return

    s = load_settings()
    if args.note:
        s["note_path"] = args.note
        save_settings(s)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), QuoteHandler)
    except OSError as e:
        print(f"START_FAIL: {e}")
        sys.exit(1)

    log_path = Path(tempfile.gettempdir()) / "quote_server.log"
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(
                f"[{datetime.now():%H:%M:%S}] listening on http://127.0.0.1:{args.port}/  "
                f"note={s.get('note_path')}  ai_key={bool(get_api_key(s))}\n"
            )
    except Exception:
        pass

    print(f"listening on http://127.0.0.1:{args.port}/")
    print(f"note = {s.get('note_path')}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped.")


if __name__ == "__main__":
    main()