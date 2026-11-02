#!/usr/bin/env python3
"""
claude-organize — Claude Code sohbetlerini (ve fork'larını) tek yerde gör.

Claude Code her sohbeti ~/.claude/projects/<proje>/<session-id>.jsonl olarak
saklar. Bu araç o dosyaları okur ve şunları gösterir:

  * hangi projede / klasörde / git branch'inde olduğu
  * son ne zaman çalışıldığı
  * ilk istek, son isteğin ve Claude'un son cevabı
  * hangi dosyalara dokunulduğu
  * hangi sohbetin hangisinden fork'landığı (ağaç olarak)
  * senin kendi notun + durum etiketi (aktif / deneme / bitti / çöp)

Kullanım:
  python3 claude_organize.py              # web arayüzünü açar (http://127.0.0.1:8765)
  python3 claude_organize.py nerdeyim     # terminalde son sohbetlerin özeti
  python3 claude_organize.py list         # tüm sohbetler (ağaç)
  python3 claude_organize.py note "logo v4 için easing deniyorum"   # en son sohbete not
  python3 claude_organize.py note <id> "not" --status deneme
  python3 claude_organize.py install-command   # Claude Code içine /nerdeyim komutunu kurar

Sadece Python 3.8+ standart kütüphanesi gerekir.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"
DATA_DIR = Path(os.environ.get("CLAUDE_ORGANIZE_DIR", Path.home() / ".claude-organize"))
NOTES_FILE = DATA_DIR / "notes.json"

STATUSES = {
    "aktif": "🟢",
    "deneme": "🧪",
    "beklemede": "⏸️",
    "bitti": "✅",
    "cop": "🗑️",
    "": "·",
}

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


# --------------------------------------------------------------------------- #
# Notlar
# --------------------------------------------------------------------------- #
_notes_lock = threading.Lock()


def load_notes() -> dict:
    try:
        return json.loads(NOTES_FILE.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_note(session_id: str, **fields) -> dict:
    with _notes_lock:
        notes = load_notes()
        entry = notes.get(session_id, {})
        for k, v in fields.items():
            if v is not None:
                entry[k] = v
        entry["updated"] = datetime.now(timezone.utc).isoformat()
        notes[session_id] = entry
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = NOTES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(notes, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(NOTES_FILE)
        return entry


# --------------------------------------------------------------------------- #
# Oturum (jsonl) okuma
# --------------------------------------------------------------------------- #
def _text_of(content) -> str:
    """message.content -> düz metin (tool çıktıları hariç)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


_NOISE = re.compile(
    r"^\s*(<command-|<local-command|<system-reminder|Caveat:|\[Request interrupted)",
)


def _is_real_prompt(text: str) -> bool:
    return bool(text.strip()) and not _NOISE.match(text)


def _clean(text: str, limit: int = 400) -> str:
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _parse_ts(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# mtime + boyut bazlı basit önbellek; büyük klasörlerde arayüz hızlı kalsın
_cache: dict[str, tuple[float, int, dict]] = {}


def parse_session(path: Path) -> dict | None:
    st = path.stat()
    cached = _cache.get(str(path))
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]

    s = {
        "id": path.stem,
        "file": str(path),
        "project_dir": path.parent.name,
        "cwd": None,
        "branch": None,
        "title": None,
        "summary": None,
        "first_prompt": None,
        "last_prompt": None,
        "last_reply": None,
        "first_ts": None,
        "last_ts": None,
        "n_user": 0,
        "n_assistant": 0,
        "files": [],
        "uuids": [],  # sıralı mesaj uuid'leri (fork tespiti için)
        "prompts": [],  # (uuid sırası, istek metni)
        "fork_hint": None,  # jsonl içinde açıkça yazıyorsa
    }
    files: dict[str, None] = {}

    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(e, dict):
                continue
            t = e.get("type")

            if t == "summary" and e.get("summary"):
                s["summary"] = e["summary"]
                continue
            for key in ("customTitle", "aiTitle", "title"):
                if isinstance(e.get(key), str) and e[key].strip():
                    if key == "customTitle" or not s["title"]:
                        s["title"] = e[key].strip()
            for key in ("forkedFrom", "forkedFromSessionId", "parentSessionId"):
                v = e.get(key)
                if isinstance(v, dict):
                    v = v.get("sessionId")
                if isinstance(v, str) and v and v != path.stem:
                    s["fork_hint"] = v

            if t not in ("user", "assistant"):
                continue
            if e.get("isSidechain"):
                continue  # alt-agent mesajları
            if e.get("uuid"):
                s["uuids"].append(e["uuid"])
            s["cwd"] = e.get("cwd") or s["cwd"]
            s["branch"] = e.get("gitBranch") or s["branch"]
            ts = _parse_ts(e.get("timestamp"))
            if ts:
                s["first_ts"] = s["first_ts"] or ts
                s["last_ts"] = ts

            msg = e.get("message") or {}
            content = msg.get("content")
            if t == "user":
                if e.get("isMeta"):
                    continue
                text = _text_of(content)
                if _is_real_prompt(text):
                    s["n_user"] += 1
                    s["first_prompt"] = s["first_prompt"] or _clean(text)
                    s["last_prompt"] = _clean(text)
                    s["prompts"].append((len(s["uuids"]) - 1, _clean(text, 160)))
            else:
                s["n_assistant"] += 1
                text = _text_of(content)
                if text.strip():
                    s["last_reply"] = _clean(text, 600)
                if isinstance(content, list):
                    for b in content:
                        if (
                            isinstance(b, dict)
                            and b.get("type") == "tool_use"
                            and b.get("name") in EDIT_TOOLS
                        ):
                            inp = b.get("input") or {}
                            fp = inp.get("file_path") or inp.get("notebook_path")
                            if fp:
                                files.pop(fp, None)
                                files[fp] = None  # en son dokunulan sona

    if not s["uuids"] and not s["summary"]:
        return None  # boş oturum
    s["files"] = list(files)[-15:]
    if not s["last_ts"]:
        s["last_ts"] = st.st_mtime
    _cache[str(path)] = (st.st_mtime, st.st_size, s)
    return s


def find_parents(sessions: list[dict]) -> None:
    """Fork ilişkilerini bul.

    Fork'lanan bir sohbet, ebeveyninin mesajlarını (aynı uuid'lerle) kopyalar.
    Bu yüzden ilk mesaj uuid'i aynı olan oturumlar aynı 'aile'dendir; ortak
    öneki en uzun olan ve daha önce başlamış olan oturum ebeveyn kabul edilir.
    """
    by_id = {s["id"]: s for s in sessions}
    for s in sessions:
        s["parent"] = None
        if s["fork_hint"] in by_id:
            s["parent"] = s["fork_hint"]

    families: dict[str, list[dict]] = {}
    for s in sessions:
        if s["uuids"]:
            families.setdefault(s["uuids"][0], []).append(s)

    for fam in families.values():
        if len(fam) < 2:
            continue
        # en eski başlayan / en kısa önek ebeveyn olmaya aday
        fam.sort(key=lambda x: (x["first_ts"] or 0, x["id"]))
        for s in fam:
            if s["parent"]:
                continue
            best, best_len = None, 0
            for other in fam:
                if other is s:
                    continue
                n = _common_prefix(s["uuids"], other["uuids"])
                # ebeveyn: ortak öneki tamamen ya da kendisinden daha erken bitenler
                is_older = (other["first_ts"] or 0, other["id"]) < (
                    s["first_ts"] or 0,
                    s["id"],
                ) or len(other["uuids"]) == n < len(s["uuids"])
                if n > best_len and is_older and not _is_descendant(by_id, other, s):
                    best, best_len = other, n
            if best:
                s["parent"] = best["id"]
                s["fork_point"] = best_len


def _is_descendant(by_id, node, ancestor) -> bool:
    seen = set()
    while node and node["id"] not in seen:
        if node["id"] == ancestor["id"]:
            return True
        seen.add(node["id"])
        node = by_id.get(node.get("parent") or "")
    return False


def _common_prefix(a: list, b: list) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def load_sessions() -> list[dict]:
    if not PROJECTS_DIR.is_dir():
        return []
    sessions = []
    for p in PROJECTS_DIR.glob("*/*.jsonl"):
        try:
            s = parse_session(p)
        except OSError:
            continue
        if s:
            sessions.append(dict(s))
    find_parents(sessions)
    notes = load_notes()
    by_id = {s["id"]: s for s in sessions}
    for s in sessions:
        # fork'un kendine özgü adı: fork noktasından sonraki ilk istek
        s["fork_prompt"] = None
        par = by_id.get(s.get("parent") or "")
        if par:
            fp = s.get("fork_point") or 0
            after = [t for i, t in s["prompts"] if i >= fp]
            s["fork_prompt"] = after[0] if after else None
            if s["title"] and s["title"] == par["title"]:
                s["title"] = None
    for s in sessions:
        n = notes.get(s["id"], {})
        s["note"] = n.get("note", "")
        s["status"] = n.get("status", "")
        s["label"] = n.get("label", "")
        s["display"] = (
            s["label"] or s["title"] or s["fork_prompt"] or s["summary"] or s["first_prompt"] or s["id"][:8]
        )
        s["project"] = s["cwd"] or _decode_project(s["project_dir"])
    sessions.sort(key=lambda x: x["last_ts"] or 0, reverse=True)
    return sessions


def _decode_project(name: str) -> str:
    return name.replace("-", "/") if name.startswith("-") else name


def public(s: dict) -> dict:
    d = {k: v for k, v in s.items() if k not in ("uuids", "file", "prompts")}
    d["n_messages"] = len(s["uuids"])
    return d


# --------------------------------------------------------------------------- #
# Terminal çıktısı
# --------------------------------------------------------------------------- #
def ago(ts: float | None) -> str:
    if not ts:
        return "?"
    d = time.time() - ts
    if d < 60:
        return "az önce"
    if d < 3600:
        return f"{int(d // 60)} dk önce"
    if d < 86400:
        return f"{int(d // 3600)} sa önce"
    return f"{int(d // 86400)} gün önce"


def _c(code: str, text: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


def resume_cmd(s: dict) -> str:
    cwd = s.get("cwd")
    base = f"claude --resume {s['id']}"
    return f'cd "{cwd}" && {base}' if cwd else base


def print_session(s: dict, indent: str = "", full: bool = True) -> None:
    icon = STATUSES.get(s["status"], "·")
    head = f"{indent}{icon} {_c('1', s['display'][:80])}"
    meta = f"  {_c('2', ago(s['last_ts']))}"
    if s.get("branch"):
        meta += _c("36", f"  ⎇ {s['branch']}")
    print(head + meta)
    pad = " " * len(indent) + "   "
    if s["note"]:
        print(pad + _c("33", "📝 " + s["note"]))
    if full:
        if s["last_prompt"]:
            print(pad + _c("2", "son istek: ") + s["last_prompt"][:140])
        if s["files"]:
            names = ", ".join(Path(f).name for f in s["files"][-4:])
            print(pad + _c("2", "dosyalar: ") + names)
        print(pad + _c("2", resume_cmd(s)))


def print_tree(sessions: list[dict], full: bool) -> None:
    by_parent: dict[str | None, list[dict]] = {}
    ids = {s["id"] for s in sessions}
    for s in sessions:
        p = s.get("parent") if s.get("parent") in ids else None
        by_parent.setdefault(p, []).append(s)

    def walk(node, depth):
        indent = "   " * (depth - 1) + ("└─ " if depth else "")
        print_session(node, indent, full)
        for ch in sorted(by_parent.get(node["id"], []), key=lambda x: x["last_ts"] or 0):
            walk(ch, depth + 1)

    projects: dict[str, list[dict]] = {}
    for s in by_parent.get(None, []):
        projects.setdefault(s["project"], []).append(s)
    for proj, roots in projects.items():
        print("\n" + _c("1;34", "📁 " + proj))
        for r in roots:
            walk(r, 0)


def cmd_nerdeyim(args) -> None:
    sessions = load_sessions()
    if args.here:
        cwd = os.getcwd()
        sessions = [s for s in sessions if (s["cwd"] or "").startswith(cwd)]
    sessions = [s for s in sessions if s["status"] != "cop"][: args.n]
    if not sessions:
        print("Hiç sohbet bulunamadı:", PROJECTS_DIR)
        return
    print(_c("1", f"Son {len(sessions)} sohbet (en yeni üstte):\n"))
    for i, s in enumerate(sessions, 1):
        parent = ""
        if s.get("parent"):
            parent = _c("35", f"  ↳ fork: {s['parent'][:8]}")
        print(_c("2", f"[{i}] {s['id'][:8]}") + parent)
        print_session(s, "", True)
        print()


def cmd_list(args) -> None:
    sessions = load_sessions()
    if not args.all:
        sessions = [s for s in sessions if s["status"] not in ("cop", "bitti")]
    print_tree(sessions, full=args.full)


def _resolve(prefix: str | None, sessions: list[dict]) -> dict:
    if not prefix:
        cwd = os.getcwd()
        here = [s for s in sessions if s["cwd"] == cwd]
        if not (here or sessions):
            sys.exit("Sohbet bulunamadı.")
        return (here or sessions)[0]
    hits = [s for s in sessions if s["id"].startswith(prefix)]
    if len(hits) != 1:
        sys.exit(f"'{prefix}' ile eşleşen {len(hits)} sohbet var; daha uzun bir id ver.")
    return hits[0]


def cmd_note(args) -> None:
    sessions = load_sessions()
    parts = list(args.text)
    sid = None
    if parts and re.fullmatch(r"[0-9a-f-]{4,36}", parts[0]) and any(
        s["id"].startswith(parts[0]) for s in sessions
    ):
        sid = parts.pop(0)
    s = _resolve(sid, sessions)
    text = " ".join(parts) or None
    if args.status and args.status not in STATUSES:
        sys.exit("durum şunlardan biri olmalı: " + ", ".join(k for k in STATUSES if k))
    save_note(s["id"], note=text, status=args.status, label=args.label)
    print(f"✔ {s['id'][:8]} güncellendi → {text or ''} {args.status or ''}".strip())


COMMAND_MD = """---
description: Bu sohbette ne yapıyorduk? Kısa özet çıkar ve claude-organize notuna kaydet.
allowed-tools: Bash(python3:*)
---
Bu sohbetin şu ana kadarki durumunu kullanıcı için ÇOK kısa özetle (Türkçe):

1. **Amaç:** tek cümle
2. **Şu an neredeyiz:** tek cümle
3. **Sıradaki adım:** tek cümle

Sonra bu özeti tek satırlık bir not hâline getir (en fazla ~120 karakter, "amaç — durum — sıradaki" biçiminde) ve şu komutla kaydet:

`python3 {script} note "<not>"`

$ARGUMENTS
"""


def cmd_install_command(args) -> None:
    target = CLAUDE_DIR / "commands" / "nerdeyim.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(COMMAND_MD.replace("{script}", str(Path(__file__).resolve())), "utf-8")
    print(f"✔ Kuruldu: {target}")
    print("  Claude Code içinde /nerdeyim yazınca sohbet kendini özetleyip not düşer.")


# --------------------------------------------------------------------------- #
# Web arayüzü
# --------------------------------------------------------------------------- #
INDEX_HTML = r"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claude Organize</title>
<style>
:root{--bg:#1f1e1d;--panel:#2a2927;--panel2:#33312e;--fg:#ece9e4;--dim:#9b968f;--acc:#d97757;
--line:#45423e;--ok:#6fbf73;--try:#c49af5;--wait:#e2b24a;--trash:#777}
@media (prefers-color-scheme: light){:root{--bg:#f6f4ef;--panel:#fff;--panel2:#efece6;--fg:#24221f;--dim:#6f6a63;--line:#ddd8cf}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 20px;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
header h1{font-size:16px;margin:0 12px 0 0}
input,select,textarea,button{font:inherit;color:inherit;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 10px}
input[type=search]{flex:1;min-width:180px}
button{cursor:pointer}button:hover{border-color:var(--acc)}
main{padding:16px 20px;max-width:1100px;margin:auto}
.proj{margin:22px 0 8px;color:var(--dim);font-weight:600;font-size:13px;word-break:break-all}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:6px 0;position:relative}
.card.fresh{border-left:3px solid var(--acc)}
.kids{margin-left:22px;border-left:1px solid var(--line);padding-left:12px}
.row{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.title{font-weight:600;font-size:15px;cursor:pointer}
.meta{color:var(--dim);font-size:12px}
.badge{font-size:11px;padding:1px 7px;border-radius:99px;background:var(--panel2);color:var(--dim)}
.fork{color:var(--try)}
.note{margin:6px 0 2px;padding:6px 10px;background:var(--panel2);border-radius:6px;color:var(--wait);white-space:pre-wrap}
.detail{display:none;margin-top:8px;font-size:13px}
.card.open>.detail{display:block}
.detail .lbl{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.04em;margin-top:8px}
.detail code{font-size:12px;background:var(--panel2);padding:2px 6px;border-radius:4px;word-break:break-all}
.edit{display:flex;gap:6px;margin-top:10px;flex-wrap:wrap}
.edit textarea{flex:1;min-width:200px;min-height:38px}
.st-aktif{color:var(--ok)}.st-deneme{color:var(--try)}.st-beklemede{color:var(--wait)}.st-bitti{color:var(--ok)}.st-cop{color:var(--trash)}
.card.st-cop,.card.st-bitti{opacity:.55}
.empty{color:var(--dim);text-align:center;margin-top:60px}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--acc);color:#fff;padding:8px 16px;border-radius:8px;display:none}
</style></head><body>
<header>
  <h1>🗂 Claude Organize</h1>
  <input type="search" id="q" placeholder="Ara: başlık, not, istek, dosya…">
  <select id="fst">
    <option value="open">Açık işler</option><option value="">Hepsi</option>
    <option value="aktif">🟢 aktif</option><option value="deneme">🧪 deneme</option>
    <option value="beklemede">⏸️ beklemede</option><option value="bitti">✅ bitti</option><option value="cop">🗑️ çöp</option>
  </select>
  <select id="view"><option value="tree">Ağaç (fork'lar)</option><option value="recent">Son kullanılan</option></select>
  <button id="reload">↻</button>
</header>
<main id="out"></main>
<div class="toast" id="toast"></div>
<script>
const ICON={aktif:"🟢",deneme:"🧪",beklemede:"⏸️",bitti:"✅",cop:"🗑️","":""};
let DATA=[],openIds=new Set(JSON.parse(localStorage.getItem("open")||"[]"));
const $=s=>document.querySelector(s);
const esc=s=>(s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function ago(t){if(!t)return"?";const d=Date.now()/1000-t;
 if(d<60)return"az önce";if(d<3600)return Math.floor(d/60)+" dk önce";
 if(d<86400)return Math.floor(d/3600)+" sa önce";return Math.floor(d/86400)+" gün önce";}
function toast(m){const t=$("#toast");t.textContent=m;t.style.display="block";setTimeout(()=>t.style.display="none",1600)}
async function load(){DATA=await (await fetch("/api/sessions")).json();render()}
function match(s){
  const f=$("#fst").value;
  if(f==="open"&&(s.status==="bitti"||s.status==="cop"))return false;
  if(f&&f!=="open"&&s.status!==f)return false;
  const q=$("#q").value.toLowerCase().trim();if(!q)return true;
  return [s.display,s.note,s.first_prompt,s.last_prompt,s.summary,s.branch,s.project,s.id,(s.files||[]).join(" ")]
    .join(" ").toLowerCase().includes(q);
}
function card(s,kidsHtml){
  const fresh=(Date.now()/1000-s.last_ts)<3600;
  const parent=s.parent?DATA.find(x=>x.id===s.parent):null;
  return `<div class="card st-${s.status} ${fresh?"fresh":""} ${openIds.has(s.id)?"open":""}" data-id="${s.id}">
   <div class="row">
     <span class="title st-${s.status}" data-toggle>${ICON[s.status]||""} ${esc(s.display)}</span>
     ${s.parent?`<span class="badge fork">⑂ fork${parent?": "+esc(parent.display.slice(0,30)):""}</span>`:""}
     ${s.branch?`<span class="badge">⎇ ${esc(s.branch)}</span>`:""}
     <span class="meta">${ago(s.last_ts)} · ${s.n_user} istek</span>
   </div>
   ${s.note?`<div class="note">📝 ${esc(s.note)}</div>`:""}
   <div class="detail">
     ${s.first_prompt?`<div class="lbl">İlk istek</div><div>${esc(s.first_prompt)}</div>`:""}
     ${s.last_prompt&&s.last_prompt!==s.first_prompt?`<div class="lbl">Son istek</div><div>${esc(s.last_prompt)}</div>`:""}
     ${s.last_reply?`<div class="lbl">Claude'un son cevabı</div><div>${esc(s.last_reply)}</div>`:""}
     ${s.files&&s.files.length?`<div class="lbl">Dokunulan dosyalar</div><div>${s.files.map(f=>`<code>${esc(f)}</code>`).join(" ")}</div>`:""}
     <div class="lbl">Devam etmek için</div>
     <div><code>${esc(s.resume)}</code> <button data-copy="${esc(s.resume)}">kopyala</button></div>
     <div class="edit">
       <input data-f="label" placeholder="Kısa isim (opsiyonel)" value="${esc(s.label)}">
       <textarea data-f="note" placeholder="Burada ne yapıyordum? Sıradaki adım ne?">${esc(s.note)}</textarea>
       <select data-f="status">${Object.keys(ICON).map(k=>`<option value="${k}" ${k===s.status?"selected":""}>${k?ICON[k]+" "+k:"— durum —"}</option>`).join("")}</select>
       <button data-save>Kaydet</button>
     </div>
     <div class="meta" style="margin-top:6px">${s.id}</div>
   </div>
   ${kidsHtml?`<div class="kids">${kidsHtml}</div>`:""}
  </div>`;
}
function render(){
  const shown=DATA.filter(match),ids=new Set(shown.map(s=>s.id));
  let html="";
  if(!shown.length){$("#out").innerHTML=`<div class="empty">Sohbet yok.</div>`;return}
  if($("#view").value==="recent"){html=shown.map(s=>`<div class="proj">${esc(s.project)}</div>`+card(s,"")).join("")}
  else{
    const kids={};shown.forEach(s=>{const p=ids.has(s.parent)?s.parent:null;(kids[p]=kids[p]||[]).push(s)});
    const lastOf=s=>Math.max(s.last_ts,...(kids[s.id]||[]).map(lastOf));
    const walk=s=>card(s,(kids[s.id]||[]).sort((a,b)=>lastOf(b)-lastOf(a)).map(walk).join(""));
    const groups={};(kids[null]||[]).forEach(s=>(groups[s.project]=groups[s.project]||[]).push(s));
    Object.entries(groups).sort((a,b)=>Math.max(...b[1].map(lastOf))-Math.max(...a[1].map(lastOf)))
      .forEach(([p,roots])=>{html+=`<div class="proj">📁 ${esc(p)}</div>`+roots.sort((a,b)=>lastOf(b)-lastOf(a)).map(walk).join("")});
  }
  $("#out").innerHTML=html;
}
document.addEventListener("click",async e=>{
  const c=e.target.closest(".card");
  if(e.target.matches("[data-toggle]")){c.classList.toggle("open");
    c.classList.contains("open")?openIds.add(c.dataset.id):openIds.delete(c.dataset.id);
    localStorage.setItem("open",JSON.stringify([...openIds]));}
  if(e.target.matches("[data-copy]")){await navigator.clipboard.writeText(e.target.dataset.copy);toast("Kopyalandı — terminale yapıştır")}
  if(e.target.matches("[data-save]")){
    const body={id:c.dataset.id};c.querySelectorAll(":scope>.detail [data-f]").forEach(el=>body[el.dataset.f]=el.value);
    await fetch("/api/note",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    toast("Kaydedildi");load();}
});
["#q","#fst","#view"].forEach(s=>$(s).addEventListener("input",render));
$("#reload").onclick=load;
load();setInterval(()=>{if(!document.activeElement.matches("textarea,input"))load()},15000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, INDEX_HTML, "text/html")
        elif path == "/api/sessions":
            out = []
            for s in load_sessions():
                d = public(s)
                d["resume"] = resume_cmd(s)
                out.append(d)
            self._send(200, json.dumps(out, ensure_ascii=False))
        else:
            self._send(404, "{}")

    def do_POST(self):
        if urlparse(self.path).path != "/api/note":
            return self._send(404, "{}")
        # sadece kendi sayfamızdan gelen istekleri kabul et
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and urlparse(origin).netloc != host:
            return self._send(403, "{}")
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, "{}")
        sid = body.get("id", "")
        if not re.fullmatch(r"[\w-]{1,80}", sid):
            return self._send(400, "{}")
        status = body.get("status")
        if status not in STATUSES:
            status = None
        entry = save_note(sid, note=body.get("note"), status=status, label=body.get("label"))
        self._send(200, json.dumps(entry, ensure_ascii=False))


def cmd_serve(args) -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Claude Organize çalışıyor → {url}   (durdurmak için Ctrl+C)")
    print(f"Okunan klasör: {PROJECTS_DIR}")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()


# --------------------------------------------------------------------------- #
def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="claude-organize", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("serve", help="web arayüzü (varsayılan)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("nerdeyim", aliases=["where"], help="son sohbetlerin kısa özeti")
    p.add_argument("-n", type=int, default=5)
    p.add_argument("--here", action="store_true", help="sadece bu klasördeki sohbetler")
    p.set_defaults(fn=cmd_nerdeyim)

    p = sub.add_parser("list", aliases=["ls"], help="tüm sohbetler, fork ağacıyla")
    p.add_argument("-a", "--all", action="store_true", help="bitti/çöp olanları da göster")
    p.add_argument("-f", "--full", action="store_true", help="detayları da göster")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("note", help="sohbete not / durum ekle ([id] metin)")
    p.add_argument("text", nargs="*")
    p.add_argument("-s", "--status", help="aktif | deneme | beklemede | bitti | cop")
    p.add_argument("-l", "--label", help="kısa isim")
    p.set_defaults(fn=cmd_note)

    p = sub.add_parser("install-command", help="Claude Code'a /nerdeyim komutunu kur")
    p.set_defaults(fn=cmd_install_command)

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        args = ap.parse_args(["serve"] + (argv or sys.argv[1:]))
    args.fn(args)


if __name__ == "__main__":
    main()
