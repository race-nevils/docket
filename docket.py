#!/usr/bin/env python3
# docket -- a keyboard-first review bench for visual evidence.
#
# One stdlib-only server: point it at folders of images (optionally described by
# cards.json sidecars), judge each card with single keystrokes, and every verdict
# is appended to a JSONL log alongside a frozen, content-addressed copy of the
# exact evidence you judged. Expensive human judgment, never lost, never mutated.
#
#   python3 docket.py demo                  # generate a synthetic deck and serve it
#   python3 docket.py serve ROOT [ROOT...]  # review your own decks
#   python3 docket.py check ROOT [ROOT...]  # parse-only: list cards, report problems
#
# Keyboard: Y yes · N no · F flag(+note) · H hold · U undo · 1-5 filter tabs.
#
# Principles the code enforces:
#   * The verdict log is append-only. Nothing ever rewrites or reorders it.
#   * A verdict freezes its evidence: the exact image bytes shown are copied into
#     a content-addressed store (sha256 names) the moment you judge them.
#   * Delete is a tombstone, not a file operation. Your input tree is read-only
#     to this tool; a deleted card is hidden by id in an append-only log and can
#     be restored by appending an "undelete" line.
import argparse
import base64
import hashlib
import json
import os
import re
import struct
import sys
import threading
import zlib
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

NAME = "docket"
VERDICTS = ("YES", "NO", "FLAG", "HOLD", "UNDO")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
CTYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
          ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf",
          ".txt": "text/plain; charset=utf-8", ".json": "application/json; charset=utf-8"}
SIDECAR = "cards.json"

LOCK = threading.Lock()
BOOT_ID = os.urandom(8).hex()      # new per process; the page polls /buildid and reloads on change
_EVIDENCE_BY_ID = {}               # id -> server-side evidence context, frozen at verdict time

THUMB_EDGE = 1400                  # cards show downscaled thumbs; huge rasters freeze the renderer
try:
    from PIL import Image          # optional -- without it, originals are served as-is
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


def sid(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def md_html(text):
    """Escaped text with `code` and **bold** rendered. Display only."""
    h = esc(text)
    h = re.sub(r"`([^`]+)`", r"<code>\1</code>", h)
    h = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", h)
    return h


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


# ---------------------------------------------------------------- deck discovery

def find_decks(roots):
    """Every deck under the roots, in stable path order.

    A deck is a directory that either holds a cards.json sidecar (found at any
    depth; its subtree is not searched further, so per-card asset folders never
    become decks of their own) or, failing that, directly contains image files
    (a "loose" deck: one card per image -- point the tool at a folder of
    screenshots and review them with zero setup). Hidden directories and any
    directory named like a data dir are skipped.
    Returns [{root, rel, dir, sidecar}] -- rel is the deck dir relative to its
    root ("." for the root itself); first root wins a duplicate rel.
    """
    decks, seen = [], set()
    for root in roots:
        rich_dirs = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames
                                 if not d.startswith(".") and not d.endswith("-data"))
            if SIDECAR in filenames:
                rich_dirs.append(Path(dirpath))
                dirnames[:] = []
        rich_rels = set()
        for d in sorted(rich_dirs, key=lambda p: p.relative_to(root).as_posix()):
            rel = d.relative_to(root).as_posix()
            rich_rels.add(rel)
            if rel in seen:
                continue
            seen.add(rel)
            try:
                side = json.loads((d / SIDECAR).read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, OSError) as e:
                side = {"_error": str(e)}
            decks.append({"root": root, "rel": rel, "dir": d, "sidecar": side})
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames
                                 if not d.startswith(".") and not d.endswith("-data"))
            dp = Path(dirpath)
            rel = dp.relative_to(root).as_posix()
            if rel in rich_rels or any(rel == rr or rel.startswith(rr + "/") for rr in rich_rels):
                continue
            if rel in seen:
                dirnames[:] = []
                continue
            imgs = [f for f in filenames if Path(f).suffix.lower() in IMAGE_EXTS]
            if imgs:
                seen.add(rel)
                decks.append({"root": root, "rel": rel, "dir": dp, "sidecar": None})
    return decks


def deck_title(deck):
    side = deck["sidecar"]
    if isinstance(side, dict) and side.get("title"):
        return str(side["title"])
    leaf = deck["dir"].name if deck["rel"] != "." else Path(deck["root"]).resolve().name
    return " ".join(w[:1].upper() + w[1:] for w in re.split(r"[-_]+", leaf) if w)


def find_under_roots(rel, roots):
    for r in roots:
        p = r / rel
        if p.is_file():
            return p
    return None


# ---------------------------------------------------------------- card assembly

def card_id(deck_rel, key):
    """Content-stable card id: a sidecar key that is already 16 lowercase hex is
    used verbatim (bring your own content hash -- the id then survives a deck
    rename or re-emit), anything else is hashed with its deck path."""
    if isinstance(key, str) and re.fullmatch(r"[0-9a-f]{16}", key):
        return key
    return sid(f"{deck_rel}#{key}")


def sidecar_cards(deck, roots, warnings):
    """One review card per entry in the deck's cards.json."""
    rel, side = deck["rel"], deck["sidecar"]
    prefix = "" if rel == "." else rel + "/"
    if not isinstance(side, dict) or not isinstance(side.get("cards"), list):
        err = side.get("_error") if isinstance(side, dict) else "top level must be an object"
        warnings.append(f"bad sidecar: {prefix}{SIDECAR} ({err or 'missing cards list'})")
        return []
    out = []
    for i, c in enumerate(side["cards"]):
        if not isinstance(c, dict):
            warnings.append(f"bad card: {prefix}{SIDECAR} entry {i} is not an object")
            continue
        cid = card_id(rel, c.get("key") or f"index{i}")
        label = str(c.get("label") or f"card {i + 1}")
        summary = str(c.get("summary") or "")
        imgs, missing = [], []
        for fn in c.get("images") or []:
            img_rel = prefix + str(fn)
            if find_under_roots(img_rel, roots):
                imgs.append({"rel": img_rel})
            else:
                missing.append(str(fn))
        links = []
        for ln in c.get("links") or []:
            if not isinstance(ln, dict) or not isinstance(ln.get("file"), str):
                continue
            f_rel = prefix + ln["file"]
            if not find_under_roots(f_rel, roots):
                missing.append(ln["file"])
                continue
            href = "/file/" + quote(f_rel, safe="/")
            page = ln.get("page")
            if isinstance(page, int) and page > 0:
                href += f"#page={page}"
            links.append({"t": str(ln.get("label") or Path(ln["file"]).name), "h": href})
        for fn in missing:
            warnings.append(f"missing file: {prefix}{fn}  (card: {label[:48]})")
        out.append({"id": cid, "deck": rel, "family": deck_title(deck), "label": label,
                    "html": md_html(summary), "images": imgs, "links": links,
                    "_ev": {"deck": rel, "key": c.get("key"), "label": label,
                            "summary": summary, "image_rels": [im["rel"] for im in imgs]}})
    return out


def loose_cards(deck):
    """A folder of images with no sidecar: one card per image, newest-name last."""
    rel = deck["rel"]
    prefix = "" if rel == "." else rel + "/"
    out = []
    for p in sorted(deck["dir"].iterdir()):
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
            continue
        img_rel = prefix + p.name
        out.append({"id": sid(f"{rel}#{p.name}"), "deck": rel, "family": deck_title(deck),
                    "label": p.name, "html": "", "images": [{"rel": img_rel}], "links": [],
                    "_ev": {"deck": rel, "key": p.name, "label": p.name,
                            "summary": "", "image_rels": [img_rel]}})
    return out


def build_items(cfg):
    """The full review feed across every deck, minus tombstoned cards. Side
    effect: refreshes the server-side id->evidence index that verdict-time
    freezing reads (evidence never travels through the client)."""
    warnings, items = [], []
    for deck in find_decks(cfg["roots"]):
        items += sidecar_cards(deck, cfg["roots"], warnings) if deck["sidecar"] is not None \
            else loose_cards(deck)
    dupes = {}
    for it in items:
        dupes.setdefault(it["id"], []).append(it["label"])
    for cid, labels in dupes.items():
        if len(labels) > 1:
            warnings.append(f"duplicate id {cid}: {' · '.join(labels[:4])} -- give each card a unique key")
    deleted = read_deleted(cfg)
    if deleted:
        items = [it for it in items if it["id"] not in deleted]
    ev_map = {}
    for it in items:
        ev_map[it["id"]] = it.pop("_ev")
    global _EVIDENCE_BY_ID
    _EVIDENCE_BY_ID = ev_map
    for n, it in enumerate(items, 1):
        it["n"] = n
    return items, warnings


# ---------------------------------------------------------------- verdict state

def read_state(path):
    """Replay the append-only log: last write per id wins; UNDO clears."""
    state = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                state[ev["id"]] = {"verdict": ev.get("verdict"), "note": ev.get("note", ""),
                                   "ts": ev.get("ts", ""), "media": ev.get("note_media", [])}
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
    return {k: v for k, v in state.items() if v["verdict"] in ("YES", "NO", "FLAG", "HOLD")}


def append_event(path, ev):
    with LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


def read_deleted(cfg):
    """Currently-tombstoned card ids. Append {"id":..., "op":"undelete"} to restore one."""
    ids, p = set(), cfg["data"] / "deleted.jsonl"
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            vid = ev.get("id")
            if not vid:
                continue
            ids.discard(vid) if ev.get("op") == "undelete" else ids.add(vid)
    return ids


# ---------------------------------------------------------------- evidence freezing

def gather_evidence(vid, cfg):
    """Freeze the card behind id `vid`: copy the exact image bytes it showed into
    the content-addressed store (evidence/<sha256><ext>; identical bytes dedupe
    to one file) and return a record of what the card claimed. None if the id has
    no current card -- the verdict still lands, just without a snapshot."""
    ctx = _EVIDENCE_BY_ID.get(vid)
    if ctx is None:
        build_items(cfg)
        ctx = _EVIDENCE_BY_ID.get(vid)
    if ctx is None:
        return None
    ev_dir = cfg["data"] / "evidence"
    images = []
    for rel in ctx.get("image_rels", []):
        p = find_under_roots(rel, cfg["roots"])
        if not p:
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        sha = hashlib.sha256(raw).hexdigest()
        dest = ev_dir / f"{sha}{p.suffix.lower() or '.png'}"
        if not dest.is_file():
            ev_dir.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
        images.append({"rel": rel, "sha256": sha, "file": dest.name})
    rec = {k: ctx[k] for k in ("deck", "key", "label", "summary") if ctx.get(k) not in (None, "")}
    rec["images"] = images
    return rec


# ---------------------------------------------------------------- note media (pasted screenshots)

NOTE_IMG_MAX = 8                       # per note -- a note is evidence, not an album
NOTE_IMG_MAXBYTES = 10 * 1024 * 1024
NOTE_IMG_SNIFF = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"),
                  (b"GIF87a", "gif"), (b"GIF89a", "gif"))


def _img_ext(raw):
    for sig, ext in NOTE_IMG_SNIFF:
        if raw.startswith(sig):
            return ext
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


def save_note_images(images, vid, media_dir):
    """Decode base64 data-URL screenshots pasted into a note; write each as
    <vid>-<sha256[:12]>.<ext> and return the bare filenames. Names are
    server-derived (never a client path), magic-byte sniffed, size/count capped:
    a malformed or non-image entry is skipped, never fatal. Content-hash names
    make a re-paste idempotent; vid is ^[0-9a-f]{16}$-validated upstream, so a
    name cannot escape media_dir."""
    out = []
    if not isinstance(images, list):
        return out
    for data in images[:NOTE_IMG_MAX]:
        if not isinstance(data, str):
            continue
        try:
            raw = base64.b64decode(data.split(",", 1)[-1], validate=True)
        except Exception:
            continue
        if not raw or len(raw) > NOTE_IMG_MAXBYTES:
            continue
        ext = _img_ext(raw)
        if not ext:
            continue
        name = f"{vid}-{hashlib.sha256(raw).hexdigest()[:12]}.{ext}"
        dest = media_dir / name
        if not dest.is_file():
            media_dir.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
        if name not in out:
            out.append(name)
    return out


# ---------------------------------------------------------------- thumbs

def thumb_for(p, cfg):
    """Downscaled jpeg for card display, cached by (path, mtime). Without PIL,
    or for small images, the original is served."""
    if not HAVE_PIL:
        return p
    try:
        key = hashlib.sha256(f"{p}|{p.stat().st_mtime_ns}|{THUMB_EDGE}".encode()).hexdigest()[:20]
        out = cfg["data"] / "thumbs" / f"{key}.jpg"
        if out.is_file():
            return out
        im = Image.open(p)
        if max(im.size) <= THUMB_EDGE:
            return p
        im.thumbnail((THUMB_EDGE, THUMB_EDGE))
        out.parent.mkdir(parents=True, exist_ok=True)
        im.convert("RGB").save(out, "JPEG", quality=85)
        return out
    except Exception:
        return p


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    cfg = None

    def log_message(self, fmt, *args):
        sys.stderr.write("  " + (fmt % args) + "\n")

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _safe_under(self, rel, roots):
        """Resolve rel to a file under one of the roots, traversal-guarded."""
        rel = rel.lstrip("/").replace("\\", "/")
        if ".." in rel.split("/"):
            return None
        for r in roots:
            p = (r / rel).resolve()
            if str(p).startswith(str(r.resolve()) + os.sep) and p.is_file():
                return p
        return None

    def _serve_file(self, p, ranged=False):
        size = p.stat().st_size
        ctype = CTYPES.get(p.suffix.lower(), "application/octet-stream")
        rng = self.headers.get("Range") if ranged else None
        m = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
        if m and (m.group(1) or m.group(2)):
            start = int(m.group(1)) if m.group(1) else max(0, size - int(m.group(2)))
            end = int(m.group(2)) if (m.group(1) and m.group(2)) else size - 1
            end = min(end, size - 1)
            if start > end:
                return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
            with open(p, "rb") as f:
                f.seek(start)
                data = f.read(end - start + 1)
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        with open(p, "rb") as f:
            data = f.read()
        self._send(200, data, ctype,
                   {"Accept-Ranges": "bytes"} if ranged else {"Cache-Control": "max-age=600"})

    def do_GET(self):
        cfg = self.cfg
        u = urlparse(self.path)
        try:
            if u.path == "/":
                items, warnings = build_items(cfg)
                state = read_state(cfg["data"] / "verdicts.jsonl")
                return self._send(200, render_page(items, warnings, state, cfg))
            if u.path == "/items.json":
                items, warnings = build_items(cfg)
                return self._send(200, json.dumps({"items": items, "warnings": warnings},
                                                  ensure_ascii=False), "application/json; charset=utf-8")
            if u.path == "/verdicts.json":
                return self._send(200, json.dumps(read_state(cfg["data"] / "verdicts.jsonl"),
                                                  ensure_ascii=False), "application/json; charset=utf-8")
            if u.path == "/buildid":
                return self._send(200, json.dumps({"id": BOOT_ID, "pid": os.getpid()}),
                                  "application/json")
            if u.path.startswith("/img/"):
                p = self._safe_under(u.path[5:], cfg["roots"])
                return self._serve_file(p) if p else self._send(404, "not found", "text/plain")
            if u.path.startswith("/thumb/"):
                p = self._safe_under(u.path[7:], cfg["roots"])
                if not p:
                    return self._send(404, "not found", "text/plain")
                return self._serve_file(thumb_for(p, cfg))
            if u.path.startswith("/file/"):
                p = self._safe_under(u.path[6:], cfg["roots"])
                return self._serve_file(p, ranged=True) if p else self._send(404, "not found", "text/plain")
            if u.path.startswith("/note-media/"):
                p = self._safe_under(u.path[12:], [cfg["data"] / "note-media"])
                return self._serve_file(p) if p else self._send(404, "not found", "text/plain")
            if u.path.startswith("/evidence/"):
                p = self._safe_under(u.path[10:], [cfg["data"] / "evidence"])
                return self._serve_file(p) if p else self._send(404, "not found", "text/plain")
            return self._send(404, "not found", "text/plain")
        except (ConnectionAbortedError, BrokenPipeError):
            pass

    def do_POST(self):
        cfg = self.cfg
        path = urlparse(self.path).path
        if path == "/delete":
            # Tombstone this card's id in an append-only log the feed filters.
            # No file under the roots is touched -- the input tree stays read-only.
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
                vid = str(body["id"])
            except (json.JSONDecodeError, KeyError, ValueError):
                return self._send(400, json.dumps({"ok": False, "err": "bad body"}), "application/json")
            if not re.fullmatch(r"[0-9a-f]{16}", vid):
                return self._send(400, json.dumps({"ok": False, "err": "bad id"}), "application/json")
            ctx = _EVIDENCE_BY_ID.get(vid) or {}
            append_event(cfg["data"] / "deleted.jsonl",
                         {"id": vid, "op": "delete", "deck": ctx.get("deck"),
                          "label": ctx.get("label"), "ts": now_iso()})
            build_items(cfg)
            return self._send(200, json.dumps({"ok": True, "id": vid}), "application/json")
        if path != "/verdict":
            return self._send(404, "not found", "text/plain")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
            vid, verdict = str(body["id"]), str(body["verdict"]).upper()
            note = str(body.get("note", ""))
        except (json.JSONDecodeError, KeyError, ValueError):
            return self._send(400, json.dumps({"ok": False, "err": "bad body"}), "application/json")
        if verdict not in VERDICTS or not re.fullmatch(r"[0-9a-f]{16}", vid):
            return self._send(400, json.dumps({"ok": False, "err": "bad verdict/id"}), "application/json")
        # note_media = filenames already on disk to keep; images = new base64 pastes.
        kept = [s for s in (body.get("note_media") or [])
                if isinstance(s, str) and re.fullmatch(rf"{vid}-[0-9a-f]{{12}}\.(png|jpg|gif|webp)", s)]
        media = kept + [n for n in save_note_images(body.get("images"), vid, cfg["data"] / "note-media")
                        if n not in kept]
        ev = {"id": vid, "verdict": verdict, "note": note, "ts": now_iso()}
        if media:
            ev["note_media"] = media
        if verdict != "UNDO":              # freeze the evidence behind a real verdict; UNDO is a retraction
            rec = gather_evidence(vid, cfg)
            if rec:
                ev["evidence"] = rec
        append_event(cfg["data"] / "verdicts.jsonl", ev)
        return self._send(200, json.dumps({"ok": True, "media": media, "ts": ev["ts"]}), "application/json")


# ---------------------------------------------------------------- page

def render_page(items, warnings, state, cfg):
    payload = {"items": items, "warnings": warnings, "state": state,
               "jsonl": str(cfg["data"] / "verdicts.jsonl")}
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return PAGE.replace("__DATA__", blob).replace("__BOOT_ID__", json.dumps(BOOT_ID))


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>docket</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --card2:#21262d; --txt:#e6edf3; --dim:#8b949e;
          --yes:#3fb950; --no:#f85149; --flag:#d29922; --hold:#a371f7; --focus:#58a6ff; }
  * { box-sizing:border-box; }
  button { font-family:inherit; }
  body { margin:0; background:var(--bg); color:var(--txt);
         font:15px/1.45 system-ui,"Segoe UI",sans-serif; }
  header { position:sticky; top:0; z-index:10; background:#010409; border-bottom:1px solid #30363d;
           padding:10px 18px; display:flex; flex-wrap:wrap; gap:14px; align-items:center; }
  header h1 { font-size:17px; margin:0 10px 0 0; letter-spacing:1px; }
  .tabs { display:flex; gap:2px; }
  .tab { background:transparent; border:1px solid transparent; border-bottom:2px solid transparent;
         color:var(--dim); padding:5px 12px; border-radius:0; cursor:pointer; font-size:13px; }
  .tab.on { border-bottom-color:var(--focus); color:var(--txt); }
  #prog { font-weight:600; }
  .keys { color:var(--dim); font-size:12.5px; margin-left:auto; }
  kbd { background:#21262d; border-radius:4px; padding:1px 6px; font-size:12px;
        font-family:ui-monospace,monospace; }
  #list { max-width:1560px; margin:0 auto; padding:14px 18px 60vh; }
  h3.family { font-size:14px; color:var(--txt); letter-spacing:.3px; font-weight:600;
              border-bottom:1px solid #30363d; padding-bottom:4px; margin:26px 0 6px; }
  .card { background:var(--card); border:1px solid #30363d; border-left:5px solid #30363d;
          border-radius:6px; padding:12px 14px; margin:10px 0; position:relative; }
  .card.focused { border-left-color:var(--focus); background:var(--card2);
                  box-shadow:0 0 0 1px var(--focus); }
  .card.vYES  { border-left-color:var(--yes); }
  .card.vNO   { border-left-color:var(--no);  opacity:.85; }
  .card.vFLAG { border-left-color:var(--flag); }
  .card.vHOLD { border-left-color:var(--hold); }
  .top { display:flex; gap:10px; align-items:baseline; flex-wrap:wrap; padding-right:84px; }
  .chip.state { position:absolute; top:12px; right:14px; }
  .chip { font-size:11px; font-weight:700; letter-spacing:1px; padding:2px 8px; border-radius:10px;
          background:#21262d; color:var(--dim); }
  .vYES  .chip.state { background:var(--yes);  color:#fff; }
  .vNO   .chip.state { background:var(--no);   color:#fff; }
  .vFLAG .chip.state { background:var(--flag); color:#222; }
  .vHOLD .chip.state { background:var(--hold); color:#fff; }
  .chip.num { background:#30363d; color:#c9d1d9; }
  .label { font-weight:650; font-size:15.5px; }
  .body { margin:8px 0 2px; color:#c9d1d9; }
  .body code { background:#21262d; padding:0 5px; border-radius:4px; font-size:12.5px; color:#79c0ff;
               font-family:ui-monospace,monospace; }
  .imgs { display:flex; flex-wrap:wrap; gap:10px; margin:10px 0 4px; }
  figure { margin:0; max-width:100%; }
  figure img { background:#fff; border:1px solid #30363d; border-radius:4px; display:block;
               max-height:480px; max-width:100%; width:auto; object-fit:contain; cursor:zoom-in; }
  .links { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }
  .links a, .links button { background:#21262d; border:1px solid #30363d; color:#79c0ff; font-size:12.5px;
        padding:3px 10px; border-radius:6px; text-decoration:none; cursor:pointer; }
  .links a:hover, .links button:hover { border-color:var(--focus); }
  .links button.bDel { color:#9a6a6a; border-color:#4a3a3a; margin-left:auto; }
  .links button.bDel:hover { border-color:var(--no); color:#d99; }
  .links button.bDel.arm { background:var(--no); color:#fff; border-color:var(--no); font-weight:700; }
  .acts { display:flex; gap:8px; margin-top:10px; align-items:center; flex-wrap:wrap; }
  .acts button { background:#21262d; border:1px solid #0d1117; border-radius:6px; padding:6px 16px;
                 font-weight:650; cursor:pointer; color:var(--dim); font-size:13px; }
  .acts .bY { color:var(--yes); } .acts .bN { color:var(--no); }
  .acts .bF { color:var(--flag); } .acts .bH { color:var(--hold); }
  .note { background:#0d1117; border:1px solid #30363d; color:var(--txt); border-radius:6px;
          padding:7px 10px; font:13px/1.45 system-ui,"Segoe UI",sans-serif; display:none;
          flex:1 0 100%; min-height:84px; resize:vertical; overflow-y:auto; margin-top:6px; }
  .note.show { display:block; }
  .noteacts { display:none; flex:1 0 100%; gap:8px; align-items:center; margin-top:6px; }
  .noteacts.show { display:flex; }
  .noteacts .bSave { background:#21262d; color:var(--yes); border:1px solid #0d1117; border-radius:6px;
                     padding:6px 16px; font-weight:650; font-size:13px; cursor:pointer; }
  .noteacts .bSkip { background:#21262d; color:var(--dim); border:1px solid #0d1117; border-radius:6px;
                     padding:6px 14px; font-size:13px; cursor:pointer; }
  .notehint { color:var(--dim); font-size:12px; }
  .chips { display:flex; flex-wrap:wrap; gap:6px; flex:1 0 100%; margin-top:6px; }
  .chips:empty { margin:0; }
  .chip-img { position:relative; display:inline-block; line-height:0; }
  .chip-img img { height:56px; width:auto; max-width:120px; object-fit:cover;
                  border:1px solid #30363d; border-radius:5px; display:block; cursor:default; }
  .chip-img .chipx { position:absolute; top:-7px; right:-7px; width:18px; height:18px; padding:0;
                     line-height:15px; border-radius:50%; border:1px solid #30363d; background:#21262d;
                     color:#c9d1d9; font-size:11px; cursor:pointer; }
  .notetxt { color:var(--flag); font-size:12.5px; }
  #banner { position:fixed; top:54px; left:50%; transform:translateX(-50%); background:var(--no);
            color:#fff; padding:8px 22px; border-radius:8px; display:none; z-index:99; }
  #toast { position:fixed; top:54px; left:50%; transform:translateX(-50%); color:#fff;
           padding:7px 20px; border-radius:8px; display:none; z-index:98; font-weight:600; }
  #warn { max-width:1560px; margin:30px auto 0; padding:0 18px; color:#b07a7a;
          font:12px/1.6 ui-monospace,monospace; }
  .empty { color:var(--dim); margin:40px 0; text-align:center; font-size:15px; }
</style></head><body>
<header>
  <h1>docket</h1>
  <span id="prog"></span>
  <div class="tabs" id="tabs"></div>
  <span class="keys"><kbd>Y</kbd> yes · <kbd>N</kbd> no · <kbd>F</kbd> flag · <kbd>H</kbd> hold · <kbd>U</kbd> undo
    · <kbd>1-5</kbd> filters</span>
</header>
<div id="banner"></div>
<div id="toast"></div>
<div id="list"></div>
<div id="warn"></div>
<script>
const D = __DATA__;
const ITEMS = D.items;
let STATE = D.state;
const FILTERS = ["pending","flagged","yes","no","hold"];
const DECIDED = new Set(["flagged","yes","no","hold"]);   // verdicted tabs sort most-recent-first
let filter = "pending", focusId = null, noteId = null;
let SEQ = 0;                                              // tiebreaks same-timestamp casts this session
const IDX = new Map(ITEMS.map((it, i) => [it.id, i]));

const vOf = id => (STATE[id] && STATE[id].verdict) || null;
const tsOf = id => (STATE[id] && STATE[id].ts) || "";     // ISO-8601: lexicographic == chronological
const seqOf = id => (STATE[id] && STATE[id].seq) || 0;
const passes = it => {
  if (it.id === noteId) return true;
  const v = vOf(it.id);
  if (filter === "pending") return !v;
  return (filter==="flagged" && v==="FLAG") || (filter==="yes" && v==="YES") ||
         (filter==="no" && v==="NO") || (filter==="hold" && v==="HOLD");
};
// In a verdicted tab the most recent verdict sits on top; pending keeps deck order.
function byRecency(a, b) {
  const ta = tsOf(a.id), tb = tsOf(b.id);
  if (ta !== tb) return ta < tb ? 1 : -1;
  const sa = seqOf(a.id), sb = seqOf(b.id);
  if (sa !== sb) return sb - sa;
  return IDX.get(a.id) - IDX.get(b.id);
}
const vis = () => {
  const list = ITEMS.filter(passes);
  return DECIDED.has(filter) ? list.sort(byRecency) : list;
};

function counts() {
  const c = {pending:0, flagged:0, yes:0, no:0, hold:0, all:ITEMS.length};
  for (const it of ITEMS) {
    const v = vOf(it.id);
    if (!v) c.pending++;
    if (v === "FLAG") c.flagged++;
    if (v === "YES") c.yes++;
    if (v === "NO") c.no++;
    if (v === "HOLD") c.hold++;
  }
  return c;
}

function banner(msg) {
  const b = document.getElementById("banner");
  b.textContent = msg; b.style.display = "block";
  setTimeout(() => b.style.display = "none", 3500);
}

function toast(msg, bg) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.style.background = bg || "#2a2f3a"; t.style.display = "block";
  clearTimeout(t._h);
  t._h = setTimeout(() => t.style.display = "none", 1700);
}

function labelOf(id) {
  const it = ITEMS.find(x => x.id === id);
  return it ? "#" + String(it.n).padStart(2, "0") : id.slice(0, 6);
}

function openBox(id, selectAll) {
  const c = document.querySelector('.card[data-id="' + id + '"]');
  if (!c) return;
  const ni = c.querySelector(".note"), na = c.querySelector(".noteacts");
  if (ni) { ni.classList.add("show"); ni.focus(); if (selectAll) ni.select(); }
  if (na) na.classList.add("show");
}

function post(id, verdict, note, then, images, media) {
  const body = {id, verdict, note: note || ""};
  if (media && media.length) body.note_media = media;
  if (images && images.length) body.images = images;
  fetch("/verdict", {method:"POST", headers:{"Content-Type":"application/json"},
                     body: JSON.stringify(body)})
    .then(r => { if (!r.ok) throw 0; return r.json(); })
    .then(j => { if (!j.ok) throw 0;
                 if (verdict === "UNDO") delete STATE[id];
                 else STATE[id] = {verdict, note: note || "", media: j.media || [], ts: j.ts || "", seq: ++SEQ};
                 then && then(); })
    .catch(() => banner("verdict NOT saved: server error, nothing advanced"));
}

function card(it) {
  const v = vOf(it.id);
  const d = document.createElement("div");
  d.className = "card" + (v ? " v"+v : "") + (it.id === focusId ? " focused" : "");
  d.dataset.id = it.id;
  const note = (STATE[it.id] && STATE[it.id].note) || "";
  d.innerHTML =
    '<div class="top"><span class="chip num">#' + String(it.n).padStart(2, "0") + '</span>' +
    '<span class="chip state">' + (v || "PENDING") + '</span>' +
    '<span class="label">' + it.label.replace(/</g,"&lt;") + '</span>' +
    (note ? '<span class="notetxt">✎ ' + note.replace(/</g,"&lt;") + '</span>' : "") + '</div>' +
    (it.html ? '<div class="body">' + it.html + '</div>' : "") +
    '<div class="imgs">' + it.images.map(im =>
      '<figure><a href="/img/' + encodeURI(im.rel) + '" target="_blank" rel="noopener">' +
      '<img loading="lazy" decoding="async" src="/thumb/' + encodeURI(im.rel) +
      '" title="click → full resolution"></a></figure>').join("") + '</div>' +
    '<div class="links">' + it.links.map(l =>
        '<a href="' + l.h + '" target="_blank" rel="noopener">' + l.t.replace(/</g,"&lt;") + ' ⧉</a>'
      ).join("") +
      '<button class="bDel" title="hide this card everywhere (tombstone by id). Click, then click again to confirm">🗑 Delete</button>' +
    '</div>' +
    '<div class="acts">' +
      '<button class="bY">YES (Y)</button><button class="bN">NO (N)</button>' +
      '<button class="bF">FLAG (F)</button><button class="bH">HOLD (H)</button><button class="bU">UNDO (U)</button>' +
      '<textarea class="note' + (it.id === noteId ? " show" : "") + '" rows="4" placeholder="note: paste a screenshot here. Submit or Ctrl+Enter pins, Esc skips">' +
        note.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;") + '</textarea>' +
      '<div class="chips"></div>' +
      '<div class="noteacts' + (it.id === noteId ? " show" : "") + '">' +
        '<button class="bSave" title="pin this note + screenshots to the card">✓ Submit</button>' +
        '<button class="bSkip" title="leave the bare verdict, no note">Skip</button>' +
        '<span class="notehint">pins to the card · persists</span>' +
      '</div>' +
    '</div>';
  d.addEventListener("click", e => { if (!e.target.closest("a,button,input,textarea")) setFocus(it.id); });
  d.querySelector(".bY").onclick = () => { setFocus(it.id); verdict("YES"); };
  d.querySelector(".bN").onclick = () => { setFocus(it.id); verdict("NO"); };
  d.querySelector(".bF").onclick = () => { setFocus(it.id); verdict("FLAG"); };
  d.querySelector(".bH").onclick = () => { setFocus(it.id); verdict("HOLD"); };
  d.querySelector(".bU").onclick = () => { setFocus(it.id); verdict("UNDO"); };
  const del = d.querySelector(".bDel");     // two-step: click arms (red), second click within 4s deletes
  del.onclick = () => {
    if (del.dataset.arm) { clearTimeout(+del.dataset.t); doDelete(it.id, labelOf(it.id)); }
    else { del.dataset.arm = "1"; del.classList.add("arm"); del.textContent = "⚠ Confirm delete";
           del.dataset.t = String(setTimeout(() => { delete del.dataset.arm; del.classList.remove("arm");
             del.textContent = "🗑 Delete"; }, 4000)); }
  };
  const inp = d.querySelector(".note");
  const chipsEl = d.querySelector(".chips");
  const pending = [];                                              // new pasted blobs, pre-save
  let kept = ((STATE[it.id] && STATE[it.id].media) || []).slice(); // saved shots still attached
  function chip(src, onX) {
    const c = document.createElement("span"); c.className = "chip-img";
    const im = document.createElement("img"); im.src = src; im.loading = "lazy"; c.appendChild(im);
    const x = document.createElement("button"); x.className = "chipx"; x.textContent = "✕"; x.title = "remove";
    x.onclick = ev => { ev.stopPropagation(); onX(); };
    c.appendChild(x); return c;
  }
  function renderChips() {
    chipsEl.innerHTML = "";
    kept.forEach(name => chipsEl.appendChild(chip("/note-media/" + encodeURIComponent(name),
      () => { kept = kept.filter(x => x !== name); renderChips(); })));
    pending.forEach(p => chipsEl.appendChild(chip(p.url,
      () => { URL.revokeObjectURL(p.url); pending.splice(pending.indexOf(p), 1); renderChips(); })));
  }
  renderChips();
  inp.addEventListener("paste", e => {
    const files = e.clipboardData && e.clipboardData.files;
    if (!files || !files.length) return;
    let got = false;
    for (const f of files) if (f.type.startsWith("image/")) {
      pending.push({blob: f, url: URL.createObjectURL(f)}); got = true; }
    if (got) { e.preventDefault(); renderChips(); }
  });
  function saveNote(then) {                       // attach note+shots to the card's CURRENT verdict
    const v = vOf(it.id) || "FLAG";               // a note on an unverdicted card implies FLAG
    const finish = imgs => post(it.id, v, inp.value.trim(), then, imgs, kept);
    if (!pending.length) return finish([]);
    const imgs = []; let done = 0;
    pending.forEach((p, i) => {
      const r = new FileReader();
      r.onload = () => { imgs[i] = r.result; if (++done === pending.length) finish(imgs.filter(Boolean)); };
      r.onerror = () => { if (++done === pending.length) finish(imgs.filter(Boolean)); };
      r.readAsDataURL(p.blob);
    });
  }
  function pinNote() {                            // Submit / Ctrl+Enter: pin note+shots, confirm, advance
    const pre = vis().findIndex(x => x.id === it.id);
    const ns = kept.length + pending.length;
    saveNote(() => { noteId = null;
      toast("✎ pinned to " + labelOf(it.id) + (ns ? " · " + ns + " shot" + (ns > 1 ? "s" : "") : ""), "#2d6a2d");
      advance(it.id, pre); });
  }
  function skipNote() {                           // Skip / Esc: leave the bare verdict, move on
    const pre = vis().findIndex(x => x.id === it.id);
    noteId = null; advance(it.id, pre);
  }
  d.querySelector(".bSave").onclick = pinNote;
  d.querySelector(".bSkip").onclick = skipNote;
  inp.addEventListener("keydown", e => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { pinNote(); e.preventDefault(); }
    else if (e.key === "Escape") { skipNote(); e.preventDefault(); }
    e.stopPropagation();
  });
  return d;
}

function render(keepScroll) {
  const c = counts();
  document.getElementById("prog").textContent =
    (c.all - c.pending) + "/" + c.all + " verdicted";
  const tabs = document.getElementById("tabs");
  tabs.innerHTML = "";
  FILTERS.forEach((f, i) => {
    const b = document.createElement("button");
    b.className = "tab" + (filter === f ? " on" : "");
    b.textContent = (i+1) + " - " + f + " (" + c[f] + ")";
    b.onclick = () => { filter = f; focusId = null; noteId = null; render(); };
    tabs.appendChild(b);
  });
  const list = document.getElementById("list");
  list.innerHTML = "";
  const v = vis();
  if (!v.length) { list.innerHTML = '<div class="empty">nothing in "' + filter + '", pick another filter (1-5)</div>'; return; }
  if (!focusId || !v.some(it => it.id === focusId)) focusId = v[0].id;
  const grouped = !DECIDED.has(filter);           // family headers only in the pending feed
  let fam = null;
  for (const it of v) {
    if (grouped && it.family !== fam) {
      fam = it.family;
      if (fam) { const h = document.createElement("h3"); h.className = "family";
                 h.textContent = fam; list.appendChild(h); }
    }
    list.appendChild(card(it));
  }
  if (!keepScroll) {
    const f = list.querySelector(".card.focused");
    if (f) f.scrollIntoView({block:"center"});
  }
  if (noteId) openBox(noteId);
}

function setFocus(id) {
  focusId = id;
  document.querySelectorAll(".card").forEach(c =>
    c.classList.toggle("focused", c.dataset.id === id));
}

function advance(fromId, preIdx) {
  // preIdx = the item's index in the PRE-verdict visible list; the item may have
  // left the filter, in which case the next item now sits at that same index.
  render(true);
  const now = vis();
  if (!now.length) { render(); return; }
  const still = now.findIndex(it => it.id === fromId);
  let t = still >= 0 ? still + 1 : (preIdx >= 0 ? preIdx : 0);
  t = Math.min(Math.max(t, 0), now.length - 1);
  focusId = now[t].id; setFocus(focusId);
  const el = document.querySelector('.card[data-id="' + focusId + '"]');
  if (el) el.scrollIntoView({block:"center"});
}

function doDelete(id, lab) {
  const pre = vis().findIndex(x => x.id === id);
  fetch("/delete", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({id})})
    .then(r => r.json())
    .then(j => {
      if (!j.ok) { banner("delete failed: " + (j.err || "server error")); return; }
      const i = ITEMS.findIndex(x => x.id === id);
      if (i >= 0) ITEMS.splice(i, 1);
      delete STATE[id];
      if (lastVerdict === id) lastVerdict = null;
      if (focusId === id) focusId = null;
      toast("🗑 " + (lab || "card") + " hidden. Restore by appending an undelete line", "#6a2d2d");
      render(true);
      const now = vis();
      if (!now.length) { render(); return; }
      const t = Math.min(Math.max(pre, 0), now.length - 1);
      focusId = now[t].id; setFocus(focusId);
      const el = document.querySelector('.card[data-id="' + focusId + '"]');
      if (el) el.scrollIntoView({block:"center"});
    })
    .catch(() => banner("delete failed: network or server, nothing removed"));
}

let lastVerdict = null;

function verdict(v) {
  if (!focusId) return;
  const id = focusId;
  if (noteId && noteId !== id) noteId = null;
  if (v === "YES" || v === "FLAG" || v === "NO" || v === "HOLD") {
    // All four pause for an OPTIONAL note + pasted screenshots: the verdict is
    // recorded NOW (safe even if you navigate away), then the note box opens.
    // Submit / Ctrl+Enter pins note+shots · Esc / Skip leaves the bare verdict.
    // No toast here: the tab count ticking + the note box opening are the
    // feedback; the box's own hint covers the note mechanics.
    post(id, v, (STATE[id] && STATE[id].note) || "", () => { lastVerdict = id; noteId = id;
      render(true);
      openBox(id, v === "FLAG"); });
    return;
  }
  if (v === "UNDO") {
    // Undo the focused card if it HAS a verdict; otherwise undo the LAST verdict
    // given -- in the pending view a verdicted card vanishes instantly, so "undo
    // what I just did" must not depend on it still being focused.
    const target = vOf(id) ? id : (lastVerdict && vOf(lastVerdict) ? lastVerdict : null);
    if (!target) { toast("nothing to undo", "#3a404d"); return; }
    post(target, "UNDO", "", () => {
      if (lastVerdict === target) lastVerdict = null;
      toast("↩ " + labelOf(target) + " back to pending", "#3a404d");
      render();
    });
    return;
  }
}

document.addEventListener("keydown", e => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  if (e.ctrlKey || e.altKey || e.metaKey) return;
  const k = e.key.toLowerCase();
  if (k === "y") verdict("YES");
  else if (k === "n") verdict("NO");
  else if (k === "f") verdict("FLAG");
  else if (k === "h") verdict("HOLD");
  else if (k === "u") verdict("UNDO");
  else if ("12345".includes(k)) { filter = FILTERS[+k - 1]; focusId = null; noteId = null; render(); }
});

if (D.warnings.length)
  document.getElementById("warn").innerHTML =
    "<b>warnings (" + D.warnings.length + ")</b><br>" + D.warnings.map(w => w.replace(/</g,"&lt;")).join("<br>");
render();

// Reload this tab when the server restarts (new boot id => fresh code/feed).
(function () {
  const BOOT = __BOOT_ID__;
  function poll() {
    fetch("/buildid", {cache:"no-store"}).then(r => r.json()).then(j => {
      if (j.id && j.id !== BOOT) { location.reload(); return; }
      setTimeout(poll, 3000);
    }).catch(() => setTimeout(poll, 1500));
  }
  setTimeout(poll, 3000);
})();
</script></body></html>
"""


# ---------------------------------------------------------------- demo fixture
# Pure-stdlib synthetic decks: PNGs are written with zlib+struct (no PIL), the
# content is deterministic (seeded), and a few mismatched shapes are planted so
# the demo review has real judgment calls in it.

def _write_png(path, w, h, px):
    """px: bytearray of h*w*3 RGB bytes."""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
    raw = b"".join(b"\x00" + bytes(px[y * w * 3:(y + 1) * w * 3]) for y in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
                     chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


class _Canvas:
    def __init__(self, w, h, bg=(246, 244, 238)):
        self.w, self.h = w, h
        self.px = bytearray(bg * (w * h))

    def dot(self, x, y, c):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i:i + 3] = bytes(c)

    def rect(self, x0, y0, x1, y1, c):
        for y in range(y0, y1):
            for x in range(x0, x1):
                self.dot(x, y, c)

    def shape(self, kind, cx, cy, r, c):
        if kind == "cross":
            t = max(1, r // 3)
            self.rect(cx - r, cy - t, cx + r + 1, cy + t + 1, c)
            self.rect(cx - t, cy - r, cx + t + 1, cy + r + 1, c)
        elif kind == "ring":
            for y in range(cy - r, cy + r + 1):
                for x in range(cx - r, cx + r + 1):
                    d2 = (x - cx) ** 2 + (y - cy) ** 2
                    if (r - max(1, r // 3)) ** 2 <= d2 <= r * r:
                        self.dot(x, y, c)
        elif kind == "diamond":
            for y in range(cy - r, cy + r + 1):
                for x in range(cx - r, cx + r + 1):
                    if abs(x - cx) + abs(y - cy) <= r:
                        self.dot(x, y, c)
        elif kind == "box":
            t = max(1, r // 3)
            self.rect(cx - r, cy - r, cx + r + 1, cy - r + t + 1, c)
            self.rect(cx - r, cy + r - t, cx + r + 1, cy + r + 1, c)
            self.rect(cx - r, cy - r, cx - r + t + 1, cy + r + 1, c)
            self.rect(cx + r - t, cy - r, cx + r + 1, cy + r + 1, c)

    def speckle(self, rng, n):
        for _ in range(n):
            g = rng.randrange(120, 210)
            self.dot(rng.randrange(self.w), rng.randrange(self.h), (g, g, g))

    def save(self, path):
        _write_png(path, self.w, self.h, self.px)


def make_demo(root):
    """Two sidecar decks (a detector's clustered candidates, mismatches planted)
    plus one loose deck (a bare folder of images). Deterministic."""
    import random
    root = Path(root)
    ink = (40, 44, 52)
    classes = [("crosses", "cross", "ring"), ("diamonds", "diamond", "box")]
    for deck_name, shape, intruder in classes:
        rng = random.Random(deck_name)
        d = root / deck_name
        d.mkdir(parents=True, exist_ok=True)
        sheet = _Canvas(760, 520)
        sheet.speckle(rng, 500)
        cards = []
        for ci in range(1, 7):
            n = rng.randrange(3, 9)
            wrong = ci in (3, 5)                    # planted judgment calls
            crop = _Canvas(96 * min(n, 4) + 16, 112)
            pts = []
            for j in range(n):
                kind = intruder if (wrong and j == 0) else shape
                r = rng.randrange(9, 15)
                sx, sy = rng.randrange(30, 730), rng.randrange(30, 490)
                sheet.shape(kind, sx, sy, r, ink)
                pts.append((sx, sy))
                if j < 4:
                    crop.shape(kind, 48 + 96 * j + rng.randrange(-6, 7),
                               52 + rng.randrange(-8, 9), r + 8, ink)
            crop.speckle(rng, 60)
            crop.save(d / f"cluster-{ci:02d}.png")
            key = hashlib.sha256(f"{deck_name}/{ci}:{pts}".encode("utf-8")).hexdigest()[:16]
            cards.append({"key": key, "label": shape,
                          "summary": f"**{n}** instances detected. Judge: are they all really a {shape}?",
                          "images": [f"cluster-{ci:02d}.png"],
                          "links": [{"label": "Full sheet", "file": "sheet.png"}]})
        sheet.save(d / "sheet.png")
        (d / SIDECAR).write_text(json.dumps(
            {"title": f"Demo: {deck_name} detector output", "cards": cards},
            indent=2) + "\n", encoding="utf-8")
    loose = root / "loose-screenshots"
    loose.mkdir(parents=True, exist_ok=True)
    rng = random.Random("loose")
    for i in range(1, 4):
        c = _Canvas(320, 200)
        c.speckle(rng, 300)
        for _ in range(rng.randrange(2, 5)):
            c.shape(rng.choice(["cross", "ring", "diamond", "box"]),
                    rng.randrange(30, 290), rng.randrange(30, 170), rng.randrange(10, 22), ink)
        c.save(loose / f"shot-{i:02d}.png")
    return root


# ---------------------------------------------------------------- main

def make_cfg(roots, data):
    roots = [Path(r) for r in roots]
    missing = [r for r in roots if not r.is_dir()]
    if missing:
        sys.exit(f"error: not a directory: {', '.join(map(str, missing))}")
    return {"roots": roots, "data": Path(data)}


def run_check(cfg):
    items, warnings = build_items(cfg)
    decks = sorted({it["deck"] for it in items})
    state = read_state(cfg["data"] / "verdicts.jsonl")
    print(f"feed: {len(items)} cards across {len(decks)} decks")
    print(f"verdicts: {cfg['data'] / 'verdicts.jsonl'} ({len(state)} current)")
    for w in warnings:
        print(f"  WARN {w}")
    for it in items:
        print(f"  #{it['n']:>3} {it['id']}  imgs={len(it['images'])}  "
              f"deck={it['deck'][:28]:<28} {it['label'][:56]}")
    return 1 if warnings else 0


def run_serve(cfg, port, host):
    items, warnings = build_items(cfg)
    decks = sorted({it["deck"] for it in items})
    print(f"feed: {len(items)} cards across {len(decks)} decks")
    for w in warnings:
        print(f"  WARN {w}")
    Handler.cfg = cfg
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"{NAME} -> http://{host}:{port}/   (Ctrl+C stops)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(prog=NAME, description="keyboard-first review bench for visual evidence")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("serve", help="serve the review UI")
    ps.add_argument("roots", nargs="*", default=["."], help="deck roots (default: .)")
    ps.add_argument("--port", type=int, default=8017)
    ps.add_argument("--host", default="127.0.0.1")
    ps.add_argument("--data", default=f"./{NAME}-data", help="verdicts + evidence dir")
    pc = sub.add_parser("check", help="parse decks and report, no server")
    pc.add_argument("roots", nargs="*", default=["."])
    pc.add_argument("--data", default=f"./{NAME}-data")
    pd = sub.add_parser("demo", help="generate a synthetic demo and serve it")
    pd.add_argument("--dir", default=f"./{NAME}-demo")
    pd.add_argument("--port", type=int, default=8017)
    a = ap.parse_args()
    if a.cmd == "check":
        sys.exit(run_check(make_cfg(a.roots, a.data)))
    if a.cmd == "demo":
        root = make_demo(a.dir)
        print(f"demo decks generated under {root}")
        run_serve(make_cfg([root], Path(a.dir) / f"{NAME}-data"), a.port, "127.0.0.1")
        return
    run_serve(make_cfg(a.roots, a.data), a.port, a.host)


if __name__ == "__main__":
    main()
