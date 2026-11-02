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
        "ctx_tokens": 0,  # son cevaptaki context büyüklüğü
        "model": None,
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
                usage = msg.get("usage") or {}
                ctx = sum(
                    int(usage.get(k) or 0)
                    for k in ("input_tokens", "cache_read_input_tokens",
                              "cache_creation_input_tokens", "output_tokens")
                )
                if ctx:
                    s["ctx_tokens"] = ctx
                s["model"] = msg.get("model") or s["model"]
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
    try:
        d["mtime"] = Path(s["file"]).stat().st_mtime
    except OSError:
        d["mtime"] = s["last_ts"]
    # dosya son ~90 sn içinde yazıldıysa Claude büyük ihtimalle şu an çalışıyor
    d["working"] = time.time() - d["mtime"] < 90
    limit = 1_000_000 if ("[1m]" in (s["model"] or "") or s["ctx_tokens"] > 200_000) else 200_000
    d["ctx_pct"] = min(100, round(100 * s["ctx_tokens"] / limit)) if s["ctx_tokens"] else None
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
<link rel="icon" href="__AVATAR__">
<style>
:root{--bg:#030a2a;--fg:#eef3ff;--dim:rgba(170,192,255,.58);--line:rgba(120,160,255,.2);--hover:rgba(60,110,255,.18);
--acc:#8fb4ff;--glow:rgba(70,130,255,.55);--note:#c9dbff;--bar:rgba(255,255,255,.1);--ok:#5d8cff;--warn:#ffc46b;--bad:#ff6b7a;color-scheme:dark}
*{box-sizing:border-box}
html{background:#020617}
body{margin:0;min-height:100vh;color:var(--fg);font:15px/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",sans-serif;
background:radial-gradient(1200px 700px at 25% -10%,#1446d8 0%,rgba(20,70,216,0) 60%),radial-gradient(900px 600px at 100% 30%,#0b2a8a 0%,rgba(11,42,138,0) 65%),linear-gradient(180deg,#05124a 0%,#020617 100%);background-attachment:fixed}
main{max-width:880px;margin:0 auto;padding:34px 20px 70px}
.top{display:flex;align-items:center;gap:14px;margin-bottom:14px}
.top h1{display:flex;align-items:center;gap:12px;font-size:17px;margin:0;font-weight:600;white-space:nowrap;letter-spacing:.01em;text-shadow:0 0 18px var(--glow)}
.av{width:46px;height:46px;border-radius:13px;border:1px solid rgba(200,222,255,.7);box-shadow:0 0 18px rgba(70,130,255,.6),inset 0 0 0 1px rgba(255,255,255,.15)}
#q{flex:1;min-width:0;font:inherit;color:inherit;background:rgba(30,70,200,.18);border:1px solid var(--line);border-radius:12px;padding:9px 13px;outline:none;box-shadow:inset 0 0 18px rgba(60,110,255,.15)}
#q:focus{border-color:rgba(190,215,255,.6);box-shadow:inset 0 0 18px rgba(60,110,255,.3),0 0 18px rgba(70,130,255,.35)}
#q::placeholder{color:var(--dim)}
.hint{font-size:12px;color:var(--dim)}
.proj{margin:30px 0 8px 4px;font-size:11px;font-weight:500;color:var(--dim);text-transform:uppercase;letter-spacing:.12em}
.proj::before{content:"▾ ";font-size:10px}
.row{display:grid;grid-template-columns:18px 1fr auto auto 26px;align-items:center;gap:12px;padding:13px 14px;margin:9px 0;cursor:pointer;
border-radius:16px;border:1px solid var(--line);
background:linear-gradient(155deg,rgba(45,95,255,.30) 0%,rgba(14,40,150,.28) 55%,rgba(6,20,90,.35) 100%);
box-shadow:inset 0 0 28px rgba(50,100,255,.22),inset 0 1px 0 rgba(255,255,255,.06);
transition:border-color .25s,box-shadow .25s,transform .25s}
.row:hover{border-color:rgba(190,215,255,.55);box-shadow:inset 0 0 34px rgba(80,140,255,.45),0 0 20px rgba(70,130,255,.35),inset 0 1px 0 rgba(255,255,255,.35)}
.row.lit{border-color:rgba(200,222,255,.7);background:linear-gradient(155deg,rgba(60,115,255,.55) 0%,rgba(20,55,190,.38) 50%,rgba(8,25,110,.45) 100%);
box-shadow:inset 0 0 36px rgba(90,150,255,.55),0 0 26px rgba(70,130,255,.45),inset 0 1px 0 rgba(255,255,255,.55)}
.row.working{border-color:rgba(225,236,255,.9);animation:glow 1.8s ease-in-out infinite;
background:linear-gradient(155deg,rgba(80,135,255,.65) 0%,rgba(25,65,210,.45) 50%,rgba(8,25,110,.5) 100%)}
@keyframes glow{0%,100%{box-shadow:inset 0 0 36px rgba(110,165,255,.6),0 0 22px rgba(80,140,255,.45),inset 0 1px 0 rgba(255,255,255,.6)}
50%{box-shadow:inset 0 0 48px rgba(140,185,255,.8),0 0 42px rgba(90,150,255,.75),inset 0 1px 0 rgba(255,255,255,.8)}}
.dot{width:7px;height:7px;border-radius:50%;background:var(--dim);opacity:.6;justify-self:center}
.dot.fresh{background:#dbe7ff;opacity:1;box-shadow:0 0 8px #9cc0ff}
.dot.working{background:#fff;opacity:1;box-shadow:0 0 10px #b8d2ff;animation:pulse 1.1s ease-in-out infinite}
@keyframes pulse{50%{transform:scale(1.7);opacity:.4}}
.name{min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:500}
.name .fk{color:var(--dim);margin-right:4px}
.name .nt{color:var(--note);margin-left:10px;font-size:13px;font-weight:400;opacity:.85}
.name .wk{color:#fff;margin-left:10px;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;text-shadow:0 0 10px #9cc0ff}
.ctx{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--dim);font-variant-numeric:tabular-nums;width:78px}
.ctx i{display:block;width:40px;height:4px;border-radius:2px;background:var(--bar);overflow:hidden}
.ctx b{display:block;height:100%;background:linear-gradient(90deg,#3b6bff,#a9c6ff);box-shadow:0 0 8px #6f9bff}
.ctx.w b{background:var(--warn);box-shadow:0 0 8px var(--warn)}.ctx.x b{background:var(--bad);box-shadow:0 0 8px var(--bad)}.ctx.x{color:var(--bad)}
.when{font-size:12px;color:var(--dim);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;min-width:96px;letter-spacing:.02em}
.row.lit .when,.row.working .when{color:rgba(225,236,255,.85)}
.act{opacity:0;border:0;background:none;color:var(--dim);cursor:pointer;font-size:15px;padding:0}
.row:hover .act{opacity:1}.act:hover{color:#fff}
.edit{grid-column:2/-1;display:flex;gap:6px}
.edit input{flex:1;font:inherit;font-size:14px;color:inherit;background:rgba(5,15,60,.6);border:1px solid rgba(190,215,255,.6);border-radius:9px;padding:6px 10px;outline:none}
.edit button{font:inherit;font-size:13px;border:1px solid var(--line);background:rgba(60,110,255,.3);color:inherit;border-radius:9px;padding:0 12px;cursor:pointer}
.menu{background:rgba(8,22,90,.92)!important;backdrop-filter:blur(12px);border-color:rgba(150,185,255,.35)!important;box-shadow:0 0 30px rgba(50,100,255,.4)!important}
.menu div{border-radius:6px}.menu div:hover{background:var(--hover)}
.done-toggle{margin-top:36px;font-size:12px;letter-spacing:.06em;color:var(--dim);cursor:pointer;background:none;border:0;padding:0}
.done .row{opacity:.45;filter:saturate(.6)}
.empty{color:var(--dim);text-align:center;margin-top:60px}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:rgba(20,60,200,.85);border:1px solid rgba(200,222,255,.6);color:#fff;padding:9px 18px;border-radius:12px;font-size:14px;display:none;box-shadow:0 0 28px rgba(70,130,255,.6);backdrop-filter:blur(10px)}
@media (max-width:560px){.row{grid-template-columns:14px 1fr auto 22px}.ctx{display:none}.when{min-width:0}}
</style></head><body>
<main>
  <div class="top"><h1><img class="av" src="__AVATAR__" alt=""> Claude Organize</h1><input type="search" id="q" placeholder="Ara…"></div>
  <div class="hint">Satıra tıkla → devam komutu kopyalanır, Terminal'e yapıştır. ✎ not yaz · ✓ bitti</div>
  <div id="out"></div>
  <button class="done-toggle" id="dt"></button>
  <div id="done" class="done"></div>
</main>
<div class="toast" id="toast"></div>
<script>
let DATA=[],editing=null,showDone=false;
const $=s=>document.querySelector(s);
const esc=s=>(s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const GUN=["Paz","Pzt","Sal","Çar","Per","Cum","Cmt"],AY=["Oca","Şub","Mar","Nis","May","Haz","Tem","Ağu","Eyl","Eki","Kas","Ara"];
function when(t){
  const d=new Date(t*1000),now=new Date(),hm=d.toTimeString().slice(0,5);
  const day=x=>new Date(x.getFullYear(),x.getMonth(),x.getDate());
  const diff=Math.round((day(now)-day(d))/864e5),sec=now/1000-t;
  const rel=sec<60?"az önce":sec<3600?Math.floor(sec/60)+" dk":"";
  if(diff===0)return rel?`${rel} · ${hm}`:`Bugün ${hm}`;
  if(diff===1)return`Dün ${hm}`;
  if(diff<7)return`${GUN[d.getDay()]} ${hm}`;
  return`${d.getDate()} ${AY[d.getMonth()]}`;
}
function toast(m){const t=$("#toast");t.textContent=m;t.style.display="block";clearTimeout(t._h);t._h=setTimeout(()=>t.style.display="none",1800)}
async function load(){if(editing)return;DATA=await (await fetch("/api/sessions")).json();render()}
async function save(id,body){await fetch("/api/note",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id,...body})});editing=null;await load()}
const short=p=>(p||"").split("/").filter(Boolean).pop()||p;
function row(s,depth){
  const t=s.mtime||s.last_ts,fresh=Date.now()/1000-t<3600;
  const c=s.ctx_pct;
  const ctx=c==null?`<span class="ctx"></span>`:`<span class="ctx ${c>=80?"x":c>=60?"w":""}" title="Sohbet hafızasının (context) doluluğu"><i><b style="width:${c}%"></b></i>%${c}</span>`;
  if(editing===s.id)return`<div class="row" data-id="${s.id}" style="cursor:default"><span class="dot"></span>
    <div class="edit"><input id="ed" value="${esc(s.note)}" placeholder="Burada ne yapıyordum? Sıradaki adım?"><button data-save>Kaydet</button></div></div>`;
  return`<div class="row ${s.working?"working":fresh?"lit":""}" data-id="${s.id}" title="${esc(s.project)}">
    <span class="dot ${s.working?"working":fresh?"fresh":""}"></span>
    <span class="name" style="padding-left:${depth*20}px">${depth?'<span class="fk">└</span>':""}${esc(s.display)}${s.working?'<span class="wk">çalışıyor…</span>':""}${s.note?`<span class="nt">📝 ${esc(s.note)}</span>`:""}</span>
    ${ctx}<span class="when">${when(t)}</span>
    <button class="act" data-${s.status==="bitti"||s.status==="cop"?"undo":"menu"} title="${s.status==="bitti"?"geri al":"not / bitti"}">${s.status==="bitti"||s.status==="cop"?"↺":"✎"}</button>
  </div>`;
}
function tree(list){
  const ids=new Set(list.map(s=>s.id)),kids={};
  list.forEach(s=>{const p=ids.has(s.parent)?s.parent:null;(kids[p]=kids[p]||[]).push(s)});
  const T=s=>s.mtime||s.last_ts,last=s=>Math.max(T(s),...(kids[s.id]||[]).map(last));
  const walk=(s,d)=>row(s,d)+(kids[s.id]||[]).sort((a,b)=>last(b)-last(a)).map(k=>walk(k,d+1)).join("");
  const g={};(kids[null]||[]).forEach(s=>(g[s.project]=g[s.project]||[]).push(s));
  return Object.entries(g).sort((a,b)=>Math.max(...b[1].map(last))-Math.max(...a[1].map(last)))
    .map(([p,r])=>`<div class="proj" title="${esc(p)}">${esc(short(p))}</div>`+r.sort((a,b)=>last(b)-last(a)).map(s=>walk(s,0)).join("")).join("");
}
function render(){
  const q=$("#q").value.toLowerCase().trim();
  const m=s=>!q||[s.display,s.note,s.project,s.first_prompt,s.last_prompt].join(" ").toLowerCase().includes(q);
  const isDone=s=>s.status==="bitti"||s.status==="cop";
  const open=DATA.filter(s=>!isDone(s)&&m(s)),done=DATA.filter(s=>isDone(s)&&m(s));
  $("#out").innerHTML=open.length?tree(open):`<div class="empty">Sohbet yok.</div>`;
  $("#dt").textContent=done.length?(showDone?"▾ ":"▸ ")+`Bitenler (${done.length})`:"";
  $("#done").innerHTML=showDone?tree(done):"";
  const ed=$("#ed");if(ed){ed.focus();ed.setSelectionRange(ed.value.length,ed.value.length)}
}
document.addEventListener("click",async e=>{
  const r=e.target.closest(".row");if(!r)return;const id=r.dataset.id,s=DATA.find(x=>x.id===id);
  if(e.target.matches("[data-save]"))return save(id,{note:$("#ed").value});
  if(e.target.closest(".edit"))return;
  if(e.target.matches("[data-undo]"))return save(id,{status:""});
  if(e.target.matches("[data-menu]")){
    const old=document.querySelector(".menu");if(old)old.remove();
    const m=document.createElement("div");m.className="menu";
    m.style.cssText=`position:absolute;z-index:5;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:4px;box-shadow:0 6px 20px #0003;font-size:14px`;
    m.innerHTML=`<div data-a="note" style="padding:6px 12px;cursor:pointer">✎ Not yaz</div><div data-a="done" style="padding:6px 12px;cursor:pointer">✓ Bitti olarak işaretle</div>`;
    const b=e.target.getBoundingClientRect();m.style.top=(scrollY+b.bottom+4)+"px";m.style.left=Math.max(8,b.right-200)+"px";
    m.onclick=ev=>{const a=ev.target.dataset.a;m.remove();if(a==="note"){editing=id;render()}if(a==="done")save(id,{status:"bitti"})};
    document.body.appendChild(m);e.stopPropagation();return;
  }
  await navigator.clipboard.writeText(s.resume);toast("Kopyalandı — Terminal'e yapıştır");
});
document.addEventListener("click",e=>{if(!e.target.closest(".menu,[data-menu]")){const m=document.querySelector(".menu");if(m)m.remove()}},true);
document.addEventListener("keydown",e=>{
  if(!editing)return;
  if(e.key==="Enter")save(editing,{note:$("#ed").value});
  if(e.key==="Escape"){editing=null;render()}
});
$("#q").addEventListener("input",render);
$("#dt").onclick=()=>{showDone=!showDone;render()};
load();setInterval(load,5000);
</script></body></html>"""


# Başlıktaki küçük profil görseli (112x112 webp, ~2 KB)
AVATAR = "data:image/webp;base64,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"
INDEX_HTML = INDEX_HTML.replace("__AVATAR__", AVATAR)


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
