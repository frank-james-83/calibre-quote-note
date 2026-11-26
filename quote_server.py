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
import hashlib
import html
import json
import os
import re
import socket
import sqlite3
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
    "calibre_config": "",
    "enter_save": "1",
    "ai_save": "1",
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


_CALIBRE_CONFIG_CACHE = None


def running_calibre_config_dir() -> str:
    """便携版 calibre 的配置在 <安装目录>\\Calibre Settings，不在 %APPDATA%。

    从正在运行的 calibre 进程推断这个目录，省得手动填路径。
    """
    global _CALIBRE_CONFIG_CACHE
    if _CALIBRE_CONFIG_CACHE:
        return _CALIBRE_CONFIG_CACHE
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        psapi = ctypes.WinDLL("psapi")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        buf = (wintypes.DWORD * 8192)()
        used = wintypes.DWORD()
        if not psapi.EnumProcesses(ctypes.byref(buf), ctypes.sizeof(buf), ctypes.byref(used)):
            return ""
        names = ("calibre.exe", "calibre-portable.exe", "ebook-viewer.exe")
        for i in range(used.value // ctypes.sizeof(wintypes.DWORD)):
            handle = kernel32.OpenProcess(0x1000, False, buf[i])  # QUERY_LIMITED_INFORMATION
            if not handle:
                continue
            try:
                size = wintypes.DWORD(2048)
                image = ctypes.create_unicode_buffer(2048)
                if not kernel32.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
                    continue
            finally:
                kernel32.CloseHandle(handle)
            exe = Path(image.value)
            if exe.name.lower() not in names:
                continue
            for cand in (exe.parent / "Calibre Settings", exe.parent.parent / "Calibre Settings"):
                if (cand / "viewer-webengine.json").exists():
                    _CALIBRE_CONFIG_CACHE = str(cand)
                    return _CALIBRE_CONFIG_CACHE
    except Exception:
        pass
    return ""


def calibre_config_dirs(s: dict) -> list:
    dirs = []
    if s.get("calibre_config"):
        dirs.append(Path(s["calibre_config"]))
    if os.environ.get("CALIBRE_CONFIG_DIRECTORY"):
        dirs.append(Path(os.environ["CALIBRE_CONFIG_DIRECTORY"]))
    for env in ("APPDATA", "LOCALAPPDATA"):
        if os.environ.get(env):
            dirs.append(Path(os.environ[env]) / "calibre")
    portable = running_calibre_config_dir()
    if portable:
        dirs.append(Path(portable))
    return dirs


def calibre_db_position(book_path: str) -> str:
    """兜底：书库 metadata.db 的 last_read_positions（新版阅读器才会写这张表）。"""
    try:
        p = Path(book_path)
        db = p.parent.parent.parent / "metadata.db"
        book_id = int(p.parent.name.rsplit("(", 1)[-1].rstrip(")"))
        if not db.exists():
            return ""
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3)
        try:
            row = con.execute(
                "SELECT cfi FROM last_read_positions WHERE book=? ORDER BY epoch DESC LIMIT 1",
                (book_id,),
            ).fetchone()
        finally:
            con.close()
        return str(row[0]) if row else ""
    except Exception:
        return ""


def calibre_reading_now(s: dict) -> dict:
    """正在读的书 + 最后阅读位置（epubcfi）。

    书名取自 viewer-webengine.json 的 session_data.standalone_recently_opened[0]，
    位置取自 viewer/annots/<sha256(书文件路径)>.json 里的 last-read。
    """
    prefs = None
    for d in calibre_config_dirs(s):
        if (d / "viewer-webengine.json").exists():
            prefs = d / "viewer-webengine.json"
            break
    if prefs is None:
        return {}
    try:
        data = json.loads(prefs.read_text(encoding="utf-8"))
        items = (data.get("session_data") or {}).get("standalone_recently_opened") or []
    except Exception:
        return {}
    if not items:
        return {}
    top = items[0]
    book = str(top.get("title") or "").strip()
    path = str(top.get("pathtoebook") or top.get("key") or "").strip()
    if not book or not path:
        return {}
    loc = ""
    try:
        key = hashlib.sha256(path.encode("utf-8")).hexdigest()
        annots = prefs.parent / "viewer" / "annots" / f"{key}.json"
        for item in json.loads(annots.read_text(encoding="utf-8")):
            if item.get("type") == "last-read" and item.get("pos"):
                loc = str(item["pos"])
                break
    except Exception:
        pass
    return {"book": book, "loc": loc or calibre_db_position(path)}


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


def format_thread_lines(items: list, ai_max: int) -> list:
    items = get_thread_items(items)
    if not items:
        return []
    lines = ["", f"**AI 回应**（{ai_max} 字以内）", "", str(items[0].get("t", "")).strip()]
    round_no = 0
    for item in items[1:]:
        if item.get("r") == "user":
            round_no += 1
            lines += ["", f"**还嘴 {round_no}**"]
        else:
            lines += ["", "**AI 再回**"]
        lines += ["", str(item.get("t", "")).strip()]
    return lines


def format_entry_body(
    quote: str,
    source: str,
    thoughts: str,
    thread: list | None = None,
    ai_max: int = 50,
    ai_reply: str = "",
) -> str:
    """一条的内文：**原文** / **出处** / **想法** / AI 往复（不含 ## 时间与分隔线）。"""
    lines = ["**原文**", ""]
    for line in quote.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        lines.append("> " + line)
    lines += ["", f"**出处**：{source}", "", "**想法**", "", thoughts]
    items = get_thread_items(thread)
    if items and items[0].get("r") == "ai":
        lines += format_thread_lines(items, ai_max)
    elif ai_reply and ai_reply.strip():
        lines += ["", f"**AI 回应**（{ai_max} 字以内）", "", ai_reply.strip()]
    return "\n".join(lines).rstrip("\n") + "\n"


def format_entry(
    quote: str,
    source: str,
    thoughts: str,
    ai_reply: str,
    ai_max: int,
    thread: list | None = None,
) -> str:
    body = format_entry_body(quote, source, thoughts, thread, ai_max, ai_reply)
    return f"---\n\n## {datetime.now():%Y-%m-%d %H:%M}\n\n{body}\n"


def add_entry(path: str, text: str) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text("# 摘录笔记\n", encoding="utf-8")
    with p.open("a", encoding="utf-8") as f:
        f.write(text)
    return str(p.resolve())


# ---------------- 历史条目：导航与就地补充 ----------------

def split_note_blocks(text: str) -> list:
    """按 --- 分隔线切块，返回 (start, end, block)，块内不含分隔线。"""
    blocks = []
    pos = 0
    for m in re.finditer(r"(?m)^---[ \t]*$", text):
        blocks.append((pos, m.start(), text[pos:m.start()]))
        pos = m.end()
        if pos < len(text) and text[pos] == "\n":
            pos += 1
    blocks.append((pos, len(text), text[pos:]))
    return blocks


def parse_note_block(block: str) -> dict | None:
    """解析一条：时间、原文、出处、想法、其余内容（AI 对话 / 补充）。"""
    ts, source, mode = "", "", ""
    quote, thoughts, rest = [], [], []
    for raw in block.split("\n"):
        line = raw.rstrip()
        if not ts:
            m = re.match(r"^##\s+(.+?)\s*$", line)
            if m:
                ts = m.group(1)
                continue
        if line == "**原文**":
            mode = "quote"
            continue
        if line.startswith("**出处**"):
            source = line.split("：", 1)[-1].strip() if "：" in line else ""
            mode = "source"
            continue
        if line == "**想法**":
            mode = "thoughts"
            continue
        if line.startswith("**"):
            mode = "other"
            rest.append(raw)
            continue
        if mode == "quote":
            if line.startswith(">"):
                quote.append(line[1:].strip())
            elif line:
                mode = "other"
                rest.append(raw)
        elif mode == "thoughts":
            thoughts.append(raw)
        elif mode == "other":
            rest.append(raw)
    if not ts:
        return None
    return {
        "ts": ts,
        "quote": "\n".join(quote).strip(),
        "source": source,
        "thoughts": "\n".join(thoughts).strip(),
        "rest": "\n".join(rest).strip(),
    }


def entries_from_text(text: str) -> list:
    entries = []
    for start, end, block in split_note_blocks(text):
        item = parse_note_block(block)
        if not item:
            continue
        item["start"], item["end"] = start, end
        entries.append(item)
    return list(reversed(entries))


def read_note_entries(path: str) -> list:
    """笔记里的历史条目，最新在前。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return []
    return entries_from_text(text)


def parse_sections(rest: str) -> list:
    """把其余内容按 **小标题** 切开，返回 (标题, 正文)。"""
    sections = []
    title, buf = "", []
    for raw in rest.split("\n"):
        line = raw.rstrip()
        if line.startswith("**") and line.count("**") >= 2:
            if title or "\n".join(buf).strip():
                sections.append((title, "\n".join(buf).strip()))
            title, buf = line, []
        else:
            buf.append(raw)
    if title or "\n".join(buf).strip():
        sections.append((title, "\n".join(buf).strip()))
    return sections


def thread_from_rest(rest: str) -> list:
    """从一条的其余内容里恢复 AI 往复，载入后可以接着追问。"""
    items = []
    for title, body in parse_sections(rest):
        if not body:
            continue
        if title.startswith("**AI 回应**"):
            items.append({"r": "ai", "t": body})
        elif title.startswith("**还嘴"):
            items.append({"r": "user", "t": body})
        elif title.startswith("**AI 再回**"):
            items.append({"r": "ai", "t": body})
    return items if items and items[0]["r"] == "ai" else []


def update_entry(
    path: str,
    index: int,
    quote: str,
    source: str,
    thoughts: str,
    thread: list | None = None,
    ai_max: int = 50,
) -> str:
    """就地改写第 index 条（0 = 最新一条），保留原时间戳，其他条目不动。"""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:
        raise ValueError(f"读不到笔记文件：{e}")
    entries = entries_from_text(text)
    if not (0 <= index < len(entries)):
        raise ValueError("没有选中要更新的条目，先用方向键翻到一条。")
    target = entries[index]
    body = format_entry_body(quote, source, thoughts, thread, ai_max)
    new_block = f"\n## {target['ts']}\n\n{body}\n"
    new_text = text[: target["start"]] + new_block + text[target["end"]:]
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    try:
        os.replace(tmp, p)
    except OSError:
        p.write_text(new_text, encoding="utf-8")
        try:
            tmp.unlink()
        except OSError:
            pass
    return str(p.resolve())


def persist_entry(
    path: str,
    index: int,
    quote: str,
    source: str,
    thoughts: str,
    thread: list | None,
    ai_max: int,
    ai_reply: str = "",
) -> tuple:
    """写回笔记：index >= 0 就地更新，否则追加新条目。返回 (entry, ts, path)。"""
    entries = read_note_entries(path)
    if 0 <= index < len(entries):
        written = update_entry(path, index, quote, source, thoughts, thread, ai_max)
        return index, entries[index]["ts"], written
    written = add_entry(path, format_entry(quote, source, thoughts, ai_reply, ai_max, thread))
    newest = read_note_entries(path)
    return 0, (newest[0]["ts"] if newest else ""), written


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
.notice{margin-top:10px;font-size:12px;color:#1d6b32;background:#eaf7ee;border:1px solid #bfe3c8;border-radius:4px;padding:6px 8px}
.nav{display:flex;align-items:center;gap:8px;margin:0 0 8px}
.nav button{flex:0 0 auto;padding:5px 9px;font-size:12px}
.nav button:disabled{opacity:.4;cursor:default}
.navpos{flex:1;text-align:center;font-size:11px;color:#777}
"""

FORM_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>摘录到笔记</title><style>__CSS__</style></head><body>
<h1>摘录到笔记</h1>
<form method="post" action="/save">
  <input type="hidden" name="entry" value="__ENTRY__">
  <div class="nav">
    <button type="submit" formaction="/nav" name="dir" value="prev" class="ghost"__PREVDIS__>&#8592; 更早</button>
    <span class="navpos">__POSITION__</span>
    <button type="submit" formaction="/nav" name="dir" value="next" class="ghost"__NEXTDIS__>更晚 &#8594;</button>
  </div>
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
  <label class="chk"><input type="checkbox" name="enter_save" value="1"__ENTERCHK__> 想法里回车即保存（Shift+回车换行）<input type="hidden" name="enter_save" value="0"></label>
  <label class="chk"><input type="checkbox" name="ai_save" value="1"__AICHK__> AI 回复后自动保存<input type="hidden" name="ai_save" value="0"></label>
  <div class="row tight">
    <button type="submit" formaction="/save" class="primary">__SAVELABEL__</button>
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
  __NOTICE__
  __AIBLOCK__
  __THREAD__
  __REBUT__
  __WARN__
</form>
<div class="path">写入：__PATH__</div>
<script>
document.getElementById('thoughts').addEventListener('keydown', function(e){
  if (e.key !== 'Enter' || e.isComposing || e.keyCode === 229) return;
  if (e.ctrlKey || e.metaKey) { e.preventDefault(); this.form.submit(); return; }
  var box = document.querySelector('input[name="enter_save"][type="checkbox"]');
  if (e.shiftKey || (box && !box.checked)) return;
  e.preventDefault();
  this.form.submit();
});
</script>
</body></html>
"""

SAVED_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>已保存</title><style>__CSS__</style></head><body>
<h1>已保存</h1>
<div class="ok">__MSG__</div>
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
  <label for="calibre">calibre 配置目录（便携版一般能自动识别，识别不到再填）</label>
  <input id="calibre" name="calibre_config" value="__CALIBRE__" placeholder="C:\\Program Green\\calibre_15096\\Calibre Settings">
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

def to_entry_index(raw) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return -1


def render_form(
    s: dict,
    text: str = "",
    book: str = "",
    loc: str = "",
    thoughts: str = "",
    max_chars: int = 50,
    thread: list | None = None,
    warn: str = "",
    entry: int = -1,
    ai_block: str = "",
    notice: str = "",
) -> str:
    items = get_thread_items(thread)
    entries = read_note_entries(s.get("note_path", ""))
    total = len(entries)
    if not total:
        entry = -1
        position = "新摘录 · 还没有存档"
        prev_dis = next_dis = " disabled"
    elif entry < 0 or entry >= total:
        entry = -1
        position = f"新摘录 · 共 {total} 条存档"
        prev_dis, next_dis = "", " disabled"
    else:
        position = f"第 {entry + 1}/{total} 条 · {entries[entry]['ts']}"
        prev_dis = " disabled" if entry >= total - 1 else ""
        next_dis = ""
    opts = "".join(
        f'<option value="{html.escape(b)}">' for b in known_books(s.get("note_path", ""))
    )
    return render(FORM_PAGE, {
        "__QUOTE__": html.escape(text),
        "__BOOK__": html.escape(book),
        "__LOC__": html.escape(loc),
        "__THOUGHTS__": html.escape(thoughts),
        "__MAXCHARS__": html.escape(str(max_chars)),
        "__BOOKS__": opts,
        "__POSITION__": position,
        "__PREVDIS__": prev_dis,
        "__NEXTDIS__": next_dis,
        "__SAVELABEL__": "保存这一条" if entry >= 0 else "保存并追加",
        "__ENTERCHK__": " checked" if s.get("enter_save") != "0" else "",
        "__AICHK__": " checked" if s.get("ai_save") != "0" else "",
        "__NOTICE__": notice,
        "__ENTRY__": str(entry),
        "__AIBLOCK__": ai_block,
        "__THREAD__": build_debate_html(items) if items else "",
        "__REBUT__": build_rebuttal_block(items) if items else "",
        "__WARN__": warn,
        "__PATH__": html.escape(s.get("note_path", "")),
    })

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
                "__CALIBRE__": html.escape(s.get("calibre_config", "")),
            }))
            return

        # 默认 / 或 /quote
        text = ""
        if "text" in query:
            text = query["text"][0]
        elif "word" in query:
            text = query["word"][0]
        reading = calibre_reading_now(s)
        auto_book = reading.get("book", "")
        book = query.get("book", [auto_book or s.get("last_book", "")])[0]
        last_loc = s.get("last_loc", "")
        if book.strip() != (s.get("last_book") or "").strip():
            last_loc = ""  # 位置跟书绑定，换书就不要沿用上一本的位置
        loc = query.get("loc", [last_loc])[0]
        if not loc and reading.get("loc") and book.strip() == auto_book:
            loc = reading["loc"]
        warn = ""
        if not get_api_key(s):
            warn = '<div class="warn">AI 回应还没启用：<a href="/setup">点这里填 DeepSeek API Key</a>，填完刷新本页。</div>'
        self._send_html(render_form(
            s,
            text=text,
            book=book,
            loc=loc,
            thoughts="",
            max_chars=int(s.get("ai_max_chars", 50)),
            warn=warn,
        ))

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
        if "enter_save" in f or "ai_save" in f:
            s["enter_save"] = "1" if f.get("enter_save") == "1" else "0"
            s["ai_save"] = "1" if f.get("ai_save") == "1" else "0"
            save_settings(s)

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
                path_note = s.get("note_path", default_note_path())
                entry_in = to_entry_index(f.get("entry"))
                updating = 0 <= entry_in < len(read_note_entries(path_note))
                entry_now, ts, written = persist_entry(
                    path_note, entry_in, quote, source, thoughts, thread, max_chars, ai_reply
                )
                msg = (
                    f"已更新第 {entry_now + 1} 条（{ts}），其他条目没有改动。"
                    if updating
                    else "这条摘录已经追加到笔记文件。"
                )
                s["last_book"] = book
                s["last_loc"] = loc
                s["ai_max_chars"] = max_chars
                save_settings(s)
                shown = build_ai_block(ai_reply, max_chars)
                if get_thread_items(thread):
                    shown = build_debate_html(thread, with_form=False)
                self._send_html(render(SAVED_PAGE, {
                    "__MSG__": html.escape(msg),
                    "__PATH__": html.escape(written),
                    "__AIBLOCK__": shown,
                }))
            except Exception as e:
                self._send_html(render(ERROR_PAGE, {"__ERROR__": html.escape(str(e))}))
            return

        if path == "/discuss":
            ai_reply = ""
            warn = ""
            notice = ""
            thread = convert_from_thread_json(f.get("thread", ""))
            entry = to_entry_index(f.get("entry"))
            source = format_source(book, loc, "")
            if not thoughts.strip():
                warn = '<div class="warn">先写一句想法，AI 才有东西可回。</div>'
            elif not get_api_key(s):
                warn = '<div class="warn">还没有配置 DeepSeek API Key：<a href="/setup">点这里填</a>（或改 quote_settings.json 的 deepseek_api_key 后重启服务）。</div>'
            else:
                try:
                    if get_thread_items(thread):
                        reply = invoke_ai_debate(s, quote, source, thoughts, thread, max_chars)
                        thread = get_thread_items(thread) + [{"r": "ai", "t": reply}]
                    else:
                        ai_reply = invoke_ai(s, quote, source, thoughts, max_chars)
                        thread = [{"r": "ai", "t": ai_reply}]
                    if quote.strip() and s.get("ai_save") == "1":
                        entry, ts, _ = persist_entry(
                            s.get("note_path", default_note_path()),
                            entry,
                            quote,
                            source,
                            thoughts,
                            thread,
                            max_chars,
                        )
                        notice = f'<div class="notice">AI 回复已自动保存（{html.escape(ts)}），接着改也算同一条。</div>'
                except Exception as e:
                    warn = '<div class="warn">AI 调用失败：' + html.escape(str(e)) + '</div>'
            self._send_html(render_form(
                s,
                text=quote,
                book=book,
                loc=loc,
                thoughts=thoughts,
                max_chars=max_chars,
                thread=thread,
                warn=warn,
                notice=notice,
                entry=entry,
            ))
            return

        if path == "/rebut":
            warn = ""
            notice = ""
            entry = to_entry_index(f.get("entry"))
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
                    if quote.strip() and s.get("ai_save") == "1":
                        entry, ts, _ = persist_entry(
                            s.get("note_path", default_note_path()),
                            entry,
                            quote,
                            source,
                            thoughts,
                            thread,
                            max_chars,
                        )
                        notice = f'<div class="notice">这一轮已自动保存（{html.escape(ts)}）。</div>'
                except Exception as e:
                    warn = '<div class="warn">AI 调用失败：' + html.escape(str(e)) + '</div>'
            self._send_html(render_form(
                s,
                text=quote,
                book=book,
                loc=loc,
                thoughts=thoughts,
                max_chars=max_chars,
                thread=thread,
                warn=warn,
                notice=notice,
                entry=entry,
            ))
            return

        if path == "/nav":
            entries = read_note_entries(s.get("note_path", ""))
            total = len(entries)
            entry = to_entry_index(f.get("entry"))
            if not total:
                entry = -1
            elif entry < 0:
                entry = 0 if f.get("dir") == "prev" else -1
            elif f.get("dir") == "prev":
                entry = min(entry + 1, total - 1)
            else:
                entry = entry - 1  # 0 -> -1 = 回到新摘录
            if entry >= 0:
                e = entries[entry]
                book, _, loc = e["source"].partition(" · ")
                self._send_html(render_form(
                    s,
                    text=e["quote"],
                    book=book,
                    loc=loc,
                    thoughts=e["thoughts"],
                    max_chars=max_chars,
                    thread=thread_from_rest(e["rest"]),
                    entry=entry,
                ))
            else:
                reading = calibre_reading_now(s)
                auto_book = reading.get("book", "")
                book = auto_book or s.get("last_book", "")
                loc = reading.get("loc", "") if book.strip() == auto_book else ""
                self._send_html(render_form(
                    s,
                    text="",
                    book=book,
                    loc=loc,
                    thoughts="",
                    max_chars=max_chars,
                    entry=-1,
                ))
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
            if "calibre_config" in f:
                s["calibre_config"] = f["calibre_config"].strip()
            save_settings(s)
            saved = '<div class="warn" style="background:#eaf7ee;border-color:#bfe3c8;color:#1d6b32">已保存。</div>'
            self._send_html(render(SETUP_PAGE, {
                "__SAVED__": saved,
                "__KEY__": html.escape(s.get("deepseek_api_key", "")),
                "__NOTE__": html.escape(s.get("note_path", "")),
                "__CHARS__": html.escape(str(s.get("ai_max_chars", 50))),
                "__CALIBRE__": html.escape(s.get("calibre_config", "")),
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
    nav_tmp = Path(tempfile.gettempdir()) / f"quote_navtest_{os.urandom(4).hex()}.md"
    add_entry(str(nav_tmp), format_entry("第一条原文", "书 · p.1", "想法一", "", 50))
    add_entry(str(nav_tmp), format_entry("第二条原文", "书 · p.2", "想法二", "", 50))
    parsed = read_note_entries(str(nav_tmp))
    print("ENTRIES_PARSED=" + str(len(parsed)) + " NEWEST_IS_2ND=" + str(parsed[0]["quote"] == "第二条原文"))
    form_html = render_form({"note_path": str(nav_tmp)}, text="x", entry=0)
    print("NAV_HTML=" + str('formaction="/nav"' in form_html and "第 1/2 条" in form_html))
    print("SAVE_LABEL_EDIT=" + str("保存这一条" in form_html))
    print("SAVE_LABEL_NEW=" + str("保存并追加" in render_form({"note_path": str(nav_tmp)})))
    update_entry(str(nav_tmp), 0, "改后的原文", "书 · p.2", "改后的想法", back, 50)
    nav_text = Path(nav_tmp).read_text(encoding="utf-8")
    after = read_note_entries(str(nav_tmp))
    print("UPDATE_KEEPS_COUNT=" + str(len(after) == len(parsed)))
    print("UPDATE_TS_KEPT=" + str(after[0]["ts"] == parsed[0]["ts"]))
    print("UPDATE_IN_PLACE=" + str(after[0]["quote"] == "改后的原文" and after[0]["thoughts"] == "改后的想法"))
    print("UPDATE_OTHER_INTACT=" + str("第一条原文" in nav_text and after[1]["quote"] == "第一条原文"))
    print("THREAD_RESTORED=" + str(len(thread_from_rest(after[0]["rest"])) == len(back)))
    print("ENTRY_INDEX=" + str(to_entry_index("-1")) + "," + str(to_entry_index("abc")))
    off_html = render_form({"note_path": str(nav_tmp), "enter_save": "0", "ai_save": "0"})
    print("TOGGLES_RENDERED=" + str('name="enter_save"' in off_html and 'name="ai_save"' in off_html))
    print("TOGGLES_UNCHECKED=" + str('value="1" checked' not in off_html))
    on_html = render_form({"note_path": str(nav_tmp)})
    print("TOGGLES_CHECKED=" + str(on_html.count('value="1" checked') == 2))
    entry_idx, ts_auto, _ = persist_entry(str(nav_tmp), -1, "第三条原文", "书 · p.3", "想法三", [], 50)
    grew = read_note_entries(str(nav_tmp))
    print("PERSIST_APPEND=" + str(entry_idx == 0 and len(grew) == len(parsed) + 1))
    entry_idx, ts_auto, _ = persist_entry(str(nav_tmp), 0, "第三条改过", "书 · p.3", "想法三", [], 50)
    same = read_note_entries(str(nav_tmp))
    print("PERSIST_UPDATE=" + str(entry_idx == 0 and len(same) == len(parsed) + 1 and same[0]["quote"] == "第三条改过"))
    try:
        nav_tmp.unlink()
    except Exception:
        pass
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