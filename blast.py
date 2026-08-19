#!/usr/bin/env python3
"""BLAST — fire files across your LAN. No cloud, no account."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from qrcodegen import QrCode

WEB = ROOT / "web" / "index.html"
DEFAULT_PORT = 7733
CHUNK_HINT = 8 * 1024 * 1024
JUNK = {".ds_store", "thumbs.db", "desktop.ini", ".localized"}


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def local_ips() -> list[str]:
    found: list[str] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        found.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    out = [ip for ip in found if not ip.startswith("127.")]
    return out or ["127.0.0.1"]


def is_loopback(addr: str) -> bool:
    return addr in {"127.0.0.1", "::1", "localhost"}


def guess_device(ua: str, local: bool, host_name: str) -> str:
    ua = ua or ""
    if local:
        return host_name or "PC"
    if "iPad" in ua or ("Macintosh" in ua and "Mobile" in ua):
        return "iPad"
    if "iPhone" in ua:
        return "iPhone"
    if "Android" in ua and "Mobile" in ua:
        return "Phone"
    if "Android" in ua:
        return "Tablet"
    return "Tablet"


def safe_name(name: str) -> str:
    name = Path(name).name.strip() or "file"
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if c in bad or ord(c) < 32 else c for c in name)
    return cleaned[:180] or "file"


def unique_path(folder: Path, name: str) -> Path:
    dest = folder / name
    if not dest.exists():
        return dest
    stem, suf = dest.stem, dest.suffix
    for i in range(2, 10_000):
        cand = folder / f"{stem} ({i}){suf}"
        if not cand.exists():
            return cand
    return folder / f"{stem}-{uuid.uuid4().hex[:6]}{suf}"


def qr_svg(text: str) -> bytes:
    qr = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    n = qr.get_size()
    border = 3
    dark = []
    for y in range(n):
        for x in range(n):
            if qr.get_module(x, y):
                dark.append(f'<rect x="{x + border}" y="{y + border}" width="1" height="1"/>')
    dim = n + border * 2
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" '
        f'shape-rendering="crispEdges" width="180" height="180">'
        f'<rect width="100%" height="100%" fill="#ffffff"/>'
        f'<g fill="#09090b">{"".join(dark)}</g></svg>'
    )
    return svg.encode("utf-8")


def content_disposition(name: str) -> str:
    safe = "".join("_" if c in '\\/"\r\n' else c for c in (name or "file"))
    if not safe.strip():
        safe = "file"
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(name or 'file')}"


def pack_zip(folder: Path, dest: Path, root_name: str) -> int:
    count = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            if path.name.lower() in JUNK or path.name.startswith("."):
                continue
            rel = path.relative_to(folder)
            zf.write(path, arcname=str(Path(root_name) / rel))
            count += 1
            if count >= 8000:
                break
    return count


def pack_files(pairs: list[tuple[Path, str]], dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    used: set[str] = set()
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for path, arc in pairs:
            if not path.is_file():
                continue
            name = arc.replace("\\", "/")
            n = 2
            key = name.lower()
            while key in used:
                stem, suf = Path(arc).stem, Path(arc).suffix
                name = f"{stem} ({n}){suf}"
                key = name.lower()
                n += 1
            used.add(key)
            zf.write(path, arcname=name)
            count += 1
            if count >= 8000:
                break
    return count


def fmt_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.0f} {unit}" if x >= 10 or unit == "B" else f"{x:.1f} {unit}"
        x /= 1024
    return f"{n} B"


class Job:
    def __init__(self, jid: str, name: str, total: int):
        self.id = jid
        self.name = name
        self.total = total
        self.done = 0
        self.t0 = time.time()
        self.finished = False
        self.error = ""
        self.phase = "sending"
        self.lock = threading.Lock()

    def add(self, n: int) -> None:
        with self.lock:
            self.done += n

    def finish(self, error: str = "") -> None:
        with self.lock:
            self.finished = True
            self.error = error
            if not error and self.total:
                self.done = max(self.done, self.total)

    def public(self) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "name": self.name,
                "total": self.total,
                "done": self.done,
                "finished": self.finished,
                "error": self.error,
                "phase": self.phase,
                "elapsed": time.time() - self.t0,
            }


class Share:
    def __init__(self, path: Path, *, kind: str, origin: str, inbox: bool = False):
        self.id = uuid.uuid4().hex[:10]
        self.path = path.resolve()
        self.kind = kind
        self.origin = origin
        self.inbox = inbox
        self.added = time.time()

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def size(self) -> int:
        try:
            return 0 if self.kind == "dir" else self.path.stat().st_size
        except OSError:
            return 0

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "kind": self.kind,
            "from": self.origin,
            "inbox": self.inbox,
        }


class Upload:
    def __init__(self, dest: Path, size: int, chunks: int, chunk: int):
        self.dest = dest
        self.part = dest.with_name(dest.name + ".blastpart")
        self.size = size
        self.chunks = max(1, chunks)
        self.chunk = chunk
        self.got: set[int] = set()
        self.lock = threading.Lock()
        self.fh = open(self.part, "wb")
        if size > 0:
            self.fh.truncate(size)

    def write(self, index: int, data: bytes) -> None:
        with self.lock:
            self.fh.seek(index * self.chunk)
            self.fh.write(data)
            self.got.add(index)

    def finish(self) -> Path:
        with self.lock:
            self.fh.flush()
            self.fh.close()
            if self.size == 0:
                self.part.replace(self.dest)
                return self.dest
            if len(self.got) < self.chunks:
                raise ValueError(f"missing chunks ({len(self.got)}/{self.chunks})")
            self.part.replace(self.dest)
            return self.dest

    def abort(self) -> None:
        with self.lock:
            try:
                self.fh.close()
            except Exception:
                pass
            try:
                self.part.unlink(missing_ok=True)
            except OSError:
                pass


class Blast:
    def __init__(self, inbox: Path, host_name: str, token: str):
        self.inbox = inbox
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.host_name = host_name
        self.token = token
        self.shares: dict[str, Share] = {}
        self.uploads: dict[str, Upload] = {}
        self.peers: dict[str, dict] = {}
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def add_path(self, raw: Path, origin: str, inbox: bool = False) -> Share | None:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            return None
        kind = "dir" if path.is_dir() else "file"
        share = Share(path, kind=kind, origin=origin, inbox=inbox)
        with self.lock:
            self.shares[share.id] = share
        return share

    def get(self, sid: str) -> Share | None:
        return self.shares.get(sid)

    def listed(self) -> tuple[list[dict], list[dict]]:
        shared, inbox = [], []
        with self.lock:
            items = list(self.shares.values())
        items.sort(key=lambda s: s.added, reverse=True)
        for s in items:
            if not s.path.exists():
                continue
            (inbox if s.inbox else shared).append(s.public())
        return shared, inbox

    def touch_peer(self, ip: str, name: str, local: bool, ua: str) -> None:
        label = (name or "").strip()[:40] or guess_device(ua, local, self.host_name)
        with self.lock:
            self.peers[ip] = {
                "ip": ip,
                "name": label,
                "local": local,
                "kind": "pc" if local else "tablet",
                "seen": time.time(),
            }

    def live_peers(self) -> list[dict]:
        now = time.time()
        with self.lock:
            for ip, peer in list(self.peers.items()):
                if now - peer["seen"] > 10:
                    del self.peers[ip]
            out = []
            for peer in self.peers.values():
                out.append({k: peer[k] for k in ("ip", "name", "local", "kind")})
        if not any(p["local"] for p in out):
            out.insert(0, {
                "ip": "pc",
                "name": self.host_name,
                "local": True,
                "kind": "pc",
            })
        return out

    def peer_name(self, ip: str, local: bool) -> str:
        with self.lock:
            hit = self.peers.get(ip)
        if hit:
            return str(hit["name"])
        return self.host_name if local else "tablet"

    def drop(self, sid: str) -> bool:
        with self.lock:
            return self.shares.pop(sid, None) is not None

    def make_job(self, jid: str, name: str, total: int) -> Job:
        job = Job(jid or uuid.uuid4().hex, name, total)
        now = time.time()
        with self.lock:
            stale = [k for k, v in self.jobs.items() if now - v.t0 > 900]
            for k in stale:
                del self.jobs[k]
            self.jobs[job.id] = job
        return job

    def get_job(self, jid: str) -> Job | None:
        return self.jobs.get(jid)

    def resolve_child(self, share: Share, rel: str) -> Path:
        root = share.path if share.kind == "dir" else share.path.parent
        rel = (rel or "").replace("\\", "/").lstrip("/")
        if not rel:
            return share.path
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            raise ValueError("bad path")
        return target


class Handler(BaseHTTPRequestHandler):
    server_version = "BLAST/1.0"
    protocol_version = "HTTP/1.1"
    timeout = 600
    app: Blast
    html: bytes
    _head_only: bool = False

    def log_message(self, fmt: str, *args) -> None:
        msg = fmt % args
        quiet = ("/api/state", "/api/files", "/api/session", "/api/qr", "/api/job", "/favicon")
        if any(s in msg for s in quiet):
            return
        sys.stderr.write(f"  {self.address_string()}  {msg}\n")

    def _token_ok(self, token: str) -> bool:
        return secrets.compare_digest(token, self.app.token)

    def _json(self, code: int, payload: dict | list) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _text(self, code: int, msg: str, ctype: str = "text/plain; charset=utf-8") -> None:
        raw = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_body(self, limit: int = 32 * 1024 * 1024) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("body too large")
        return self.rfile.read(n) if n else b""

    def _public_url(self) -> str:
        ips = local_ips()
        host = ips[0] if ips else "127.0.0.1"
        return f"http://{host}:{self.server.server_port}/r/{self.app.token}/"

    def do_HEAD(self) -> None:
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        parts = [unquote(p) for p in parsed.path.split("/") if p]
        qs = parse_qs(parsed.query)

        if parsed.path in {"/", "/r", "/r/"}:
            if is_loopback(self.client_address[0]):
                self.send_response(302)
                self.send_header("Location", f"/r/{self.app.token}/")
                self.end_headers()
                return
            self._text(403, "Open the BLAST link shown on the PC.")
            return

        if len(parts) < 2 or parts[0] != "r" or not self._token_ok(parts[1]):
            self._text(403, "Wrong or missing token.")
            return

        rest = parts[2:]
        if not rest or rest == [""] or rest[0] not in {"api"}:
            try:
                page = WEB.read_bytes()
            except OSError:
                page = self.html
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page)
            return

        route = rest[1:]
        if route == ["session"]:
            local = is_loopback(self.client_address[0])
            name = qs.get("name", [""])[0]
            self.app.touch_peer(self.client_address[0], name, local, self.headers.get("User-Agent") or "")
            self._json(200, {
                "token": self.app.token,
                "local": local,
                "host": self.app.host_name,
                "inbox": str(self.app.inbox),
                "url": self._public_url(),
                "ips": local_ips(),
                "you": self.app.peer_name(self.client_address[0], local),
                "peers": self.app.live_peers(),
            })
            return
        if route == ["state"]:
            local = is_loopback(self.client_address[0])
            name = qs.get("name", [""])[0]
            self.app.touch_peer(self.client_address[0], name, local, self.headers.get("User-Agent") or "")
            shared, inbox = self.app.listed()
            self._json(200, {
                "shared": shared,
                "inbox": inbox,
                "peers": self.app.live_peers(),
                "you": self.app.peer_name(self.client_address[0], local),
                "host": self.app.host_name,
                "inbox_path": str(self.app.inbox),
            })
            return
        if route == ["qr"]:
            raw = qr_svg(self._public_url())
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return
        if route == ["files"]:
            shared, inbox = self.app.listed()
            self._json(200, {"shared": shared, "inbox": inbox})
            return
        if route[:1] == ["browse"] and len(route) == 2:
            share = self.app.get(route[1])
            if not share or share.kind != "dir":
                self._json(404, {"error": "folder gone"})
                return
            try:
                rel = qs.get("p", [""])[0]
                folder = self.app.resolve_child(share, rel)
                if not folder.is_dir():
                    self._json(400, {"error": "not a folder"})
                    return
                entries = []
                for child in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                    if child.name.lower() in JUNK or child.name.startswith("."):
                        continue
                    rel_child = str((Path(rel) / child.name)).replace("\\", "/") if rel else child.name
                    try:
                        st = child.stat()
                    except OSError:
                        continue
                    entries.append({
                        "name": child.name,
                        "dir": child.is_dir(),
                        "size": 0 if child.is_dir() else st.st_size,
                        "path": rel_child.replace("\\", "/"),
                    })
                    if len(entries) >= 800:
                        break
                self._json(200, {"name": share.name, "path": rel, "entries": entries})
            except ValueError:
                self._json(400, {"error": "bad path"})
            return
        if route[:1] == ["job"] and len(route) == 2:
            job = self.app.get_job(route[1])
            if not job:
                self._json(404, {"error": "no job"})
                return
            self._json(200, job.public())
            return
        if route[:1] == ["zip"] and len(route) == 2:
            share = self.app.get(route[1])
            if not share:
                self._json(404, {"error": "folder gone"})
                return
            try:
                rel = qs.get("p", [""])[0]
                folder = self.app.resolve_child(share, rel) if rel or share.kind == "dir" else None
                if folder is None or not folder.is_dir():
                    self._json(400, {"error": "not a folder"})
                    return
            except ValueError:
                self._json(400, {"error": "bad path"})
                return
            zip_name = f"{folder.name}.zip"
            if self._head_only:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", content_disposition(zip_name))
                self.send_header("Connection", "close")
                self.end_headers()
                return
            job_id = (qs.get("job", [""])[0] or "").strip()
            self._pack_and_send(zip_name, job_id, lambda dest: pack_zip(folder, dest, folder.name))
            return
        if route == ["bundle"]:
            ids = qs.get("id", [])
            rels_by = qs.get("p", [])
            pairs: list[tuple[Path, str]] = []
            if ids and not rels_by:
                for sid in ids:
                    sh = self.app.get(sid)
                    if not sh or not sh.path.exists():
                        continue
                    if sh.kind == "file":
                        pairs.append((sh.path, sh.name))
            elif ids and rels_by and len(ids) == 1:
                sh = self.app.get(ids[0])
                if sh:
                    for rel in rels_by:
                        try:
                            target = self.app.resolve_child(sh, rel)
                        except ValueError:
                            continue
                        if target.is_file():
                            pairs.append((target, target.name))
            if not pairs:
                self._json(400, {"error": "nothing to zip"})
                return
            zip_name = "blast-selected.zip"
            if self._head_only:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", content_disposition(zip_name))
                self.send_header("Connection", "close")
                self.end_headers()
                return
            job_id = (qs.get("job", [""])[0] or "").strip()
            self._pack_and_send(zip_name, job_id, lambda dest: pack_files(pairs, dest))
            return
        if route[:1] == ["dl"] and len(route) == 2:
            share = self.app.get(route[1])
            if not share:
                self._json(404, {"error": "file gone"})
                return
            try:
                rel = qs.get("p", [""])[0]
                target = self.app.resolve_child(share, rel) if rel or share.kind == "dir" else share.path
                if share.kind == "dir" and not rel:
                    self._json(400, {"error": "pick a file inside the folder"})
                    return
                if not target.is_file():
                    self._json(404, {"error": "not a file"})
                    return
            except ValueError:
                self._json(400, {"error": "bad path"})
                return
            job_id = (qs.get("job", [""])[0] or "").strip()
            job = None
            if job_id and not self._head_only:
                job = self.app.make_job(job_id, target.name, target.stat().st_size)
            self._send_file(target, job=job)
            return

        self._json(404, {"error": "nope"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = [unquote(p) for p in parsed.path.split("/") if p]
        if len(parts) < 4 or parts[0] != "r" or not self._token_ok(parts[1]) or parts[2] != "api":
            self._json(403, {"error": "forbidden"})
            return
        route = parts[3:]

        if route == ["unshare"]:
            try:
                body = json.loads(self._read_body(65536) or b"{}")
            except (ValueError, json.JSONDecodeError):
                body = {}
            ok = self.app.drop(str(body.get("id") or ""))
            self._json(200, {"ok": ok})
            return

        if route == ["reveal"]:
            if not is_loopback(self.client_address[0]):
                self._json(403, {"error": "PC only"})
                return
            try:
                body = json.loads(self._read_body(65536) or b"{}")
            except (ValueError, json.JSONDecodeError):
                body = {}
            if body.get("inbox"):
                path = self.app.inbox
            else:
                share = self.app.get(str(body.get("id") or ""))
                path = share.path if share else None
            if not path or not Path(path).exists():
                self._json(404, {"error": "gone"})
                return
            try:
                if sys.platform == "win32":
                    if Path(path).is_file():
                        subprocess.Popen(["explorer", f"/select,{path}"])
                    else:
                        os.startfile(path)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["xdg-open", str(path)])
            except OSError as e:
                self._json(500, {"error": str(e)})
                return
            self._json(200, {"ok": True})
            return

        if route == ["pick"]:
            if not is_loopback(self.client_address[0]):
                self._json(403, {"error": "disk picker is PC-only"})
                return
            try:
                body = json.loads(self._read_body(65536) or b"{}")
            except (ValueError, json.JSONDecodeError):
                body = {}
            paths = pick_native(bool(body.get("folder")))
            added = 0
            for p in paths:
                if self.app.add_path(Path(p), origin="this PC"):
                    added += 1
            self._json(200, {"added": added})
            return

        if route[:1] == ["upload"] and len(route) == 2:
            uid = route[1]
            try:
                meta = json.loads(self._read_body(1_000_000))
            except (ValueError, json.JSONDecodeError):
                self._json(400, {"error": "bad json"})
                return
            name = safe_name(str(meta.get("name") or "file"))
            size = int(meta.get("size") or 0)
            chunks = int(meta.get("chunks") or 1)
            chunk = int(meta.get("chunk") or CHUNK_HINT)
            if size < 0 or size > 1024 * 1024 * 1024 * 512:
                self._json(400, {"error": "ridiculous size"})
                return
            dest = unique_path(self.app.inbox, name)
            try:
                up = Upload(dest, size, chunks, chunk)
            except OSError as e:
                self._json(500, {"error": f"cannot create file: {e}"})
                return
            with self.app.lock:
                self.app.uploads[uid] = up
            self._json(200, {"ok": True, "name": dest.name})
            return

        if route[:1] == ["upload"] and len(route) == 3 and route[2] == "finish":
            uid = route[1]
            up = self.app.uploads.get(uid)
            if not up:
                self._json(404, {"error": "unknown upload"})
                return
            try:
                dest = up.finish()
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            local = is_loopback(self.client_address[0])
            origin = self.app.peer_name(self.client_address[0], local)
            self.app.add_path(dest, origin=origin, inbox=True)
            with self.app.lock:
                self.app.uploads.pop(uid, None)
            self._json(200, {"ok": True, "name": dest.name, "size": dest.stat().st_size})
            return

        self._json(404, {"error": "nope"})

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        parts = [unquote(p) for p in parsed.path.split("/") if p]
        if len(parts) != 6 or parts[0] != "r" or not self._token_ok(parts[1]) or parts[2] != "api" or parts[3] != "upload":
            self._json(403, {"error": "forbidden"})
            return
        uid, idx_s = parts[4], parts[5]
        try:
            index = int(idx_s)
        except ValueError:
            self._json(400, {"error": "bad chunk"})
            return
        up = self.app.uploads.get(uid)
        if not up:
            self._json(404, {"error": "unknown upload"})
            return
        n = int(self.headers.get("Content-Length") or 0)
        if n > up.chunk + 4096:
            self._json(400, {"error": "chunk too big"})
            return
        data = self.rfile.read(n) if n else b""
        try:
            up.write(index, data)
        except OSError as e:
            self._json(500, {"error": str(e)})
            return
        self._json(200, {"ok": True})

    def _pack_and_send(self, zip_name: str, job_id: str, build) -> None:
        job = self.app.make_job(job_id, zip_name, 0) if job_id else None
        if job:
            job.phase = "packing"
        tmp = ROOT / ".zips"
        zpath = tmp / f"{uuid.uuid4().hex}.zip"
        try:
            build(zpath)
            size = zpath.stat().st_size
        except OSError as e:
            if job:
                job.finish(str(e))
            try:
                zpath.unlink(missing_ok=True)
            except OSError:
                pass
            self._json(500, {"error": f"zip failed: {e}"})
            return
        if job:
            job.total = size
            job.phase = "sending"
        try:
            self._send_file(zpath, job=job, download_name=zip_name)
        finally:
            try:
                zpath.unlink(missing_ok=True)
            except OSError:
                pass

    def _send_file(self, path: Path, job: Job | None = None, download_name: str | None = None) -> None:
        try:
            size = path.stat().st_size
            fh = open(path, "rb")
        except OSError:
            if job:
                job.finish("unreadable")
            self._json(404, {"error": "unreadable"})
            return
        name = download_name or path.name
        # Full file only. Partial Range replies were leaving Android downloads stuck
        # in the 3-slot queue so later saves failed.
        start, end, code = 0, max(size - 1, 0), 200
        length = size
        self.send_response(code)
        # octet-stream + close: Android Chrome won't preview, and won't hang at 100% on keep-alive
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Disposition", content_disposition(name))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self._head_only:
            fh.close()
            return
        if job:
            with job.lock:
                job.done = start
                job.total = size
                job.phase = "sending"
                job.finished = False
                job.error = ""
        try:
            fh.seek(start)
            left = length
            while left > 0:
                buf = fh.read(min(256 * 1024, left))
                if not buf:
                    break
                self.wfile.write(buf)
                left -= len(buf)
                if job:
                    job.add(len(buf))
            self.wfile.flush()
            try:
                self.connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            if job:
                job.finish()
        except (ConnectionError, BrokenPipeError, OSError) as e:
            if job:
                job.finish(str(e) or "connection dropped")
        finally:
            fh.close()

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionError, BrokenPipeError, TimeoutError, OSError):
            pass


def pick_native(folder: bool) -> list[str]:
    if sys.platform == "win32":
        return pick_windows(folder)
    return pick_tk(folder)


def pick_windows(folder: bool) -> list[str]:
    if folder:
        ps = r"""
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = 'BLAST — share this folder in place'
$d.ShowNewFolderButton = $false
if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $d.SelectedPath }
"""
    else:
        ps = r"""
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Multiselect = $true
$d.Title = 'BLAST — share files in place'
if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $d.FileNames }
"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def pick_tk(folder: bool) -> list[str]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return []
    root = tk.Tk()
    root.withdraw()
    try:
        root.wm_attributes("-topmost", 1)
    except Exception:
        pass
    if folder:
        picked = filedialog.askdirectory(title="BLAST — share this folder")
        root.destroy()
        return [picked] if picked else []
    picked = filedialog.askopenfilenames(title="BLAST — share files")
    root.destroy()
    return list(picked)


def try_firewall(port: int) -> str:
    if sys.platform != "win32":
        return ""
    name = "BLAST LAN"
    check = subprocess.run(
        ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
        capture_output=True, text=True,
    )
    if check.returncode == 0 and "Enabled" in (check.stdout or ""):
        return "firewall rule already present"
    add = subprocess.run(
        [
            "netsh", "advfirewall", "firewall", "add", "rule",
            f"name={name}", "dir=in", "action=allow", "protocol=TCP",
            f"localport={port}", "profile=private",
        ],
        capture_output=True, text=True,
    )
    if add.returncode == 0:
        return "opened Windows firewall on this port (private profile)"
    return (
        "Windows firewall may block the tablet. In an Admin PowerShell:\n"
        f"    netsh advfirewall firewall add rule name=\"BLAST LAN\" dir=in action=allow protocol=TCP localport={port} profile=private"
    )


class BlastServer(ThreadingHTTPServer):
    request_queue_size = 128

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        except OSError:
            pass
        super().server_bind()


def bind_server(host: str, port: int) -> ThreadingHTTPServer:
    last = None
    candidates = [port] if port else list(range(DEFAULT_PORT, DEFAULT_PORT + 12))
    for p in candidates:
        try:
            httpd = BlastServer((host, p), Handler)
            httpd.daemon_threads = True
            return httpd
        except OSError as e:
            last = e
    raise SystemExit(f"could not bind port(s): {last}")


def banner(url: str, inbox: Path, ips: list[str], extra: str) -> None:
    lines = [
        "",
        "  ╔══════════════════════════════════════════════════════╗",
        "  ║  B L A S T                                           ║",
        "  ║  drop files here, open the link on the tablet        ║",
        "  ╚══════════════════════════════════════════════════════╝",
        "",
        f"  link    {url}",
        f"  inbox   {inbox}",
    ]
    if len(ips) > 1:
        lines.append("  also    " + "  ".join(f"http://{ip}:{url.split(':')[2].split('/')[0]}" for ip in ips[1:3]))
    if extra:
        for row in extra.splitlines():
            lines.append(f"  {row}")
    lines.append("  stop    Ctrl+C")
    lines.append("")
    print("\n".join(lines), flush=True)


def main() -> int:
    _utf8_stdio()
    parser = argparse.ArgumentParser(description="BLAST — LAN file cannon")
    parser.add_argument("paths", nargs="*", help="files or folders to share in place")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--inbox", default=str(Path.home() / "blast-inbox"))
    parser.add_argument("--name", default=socket.gethostname() or "PC")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    if not WEB.is_file():
        raise SystemExit(f"missing UI file: {WEB}")

    token = secrets.token_urlsafe(6).replace("-", "x").replace("_", "k")
    app = Blast(Path(args.inbox), args.name, token)
    missing = []
    for raw in args.paths:
        if not app.add_path(Path(raw), origin="this PC"):
            missing.append(raw)
    if missing:
        print("  skipped (not found): " + ", ".join(missing), flush=True)

    Handler.app = app
    Handler.html = WEB.read_bytes()

    httpd = bind_server(args.host, args.port)
    ips = local_ips()
    url = f"http://{ips[0]}:{httpd.server_port}/r/{token}/"
    extra = try_firewall(httpd.server_port)
    banner(url, app.inbox, ips, extra)

    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(f"http://127.0.0.1:{httpd.server_port}/r/{token}/")).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  blast stopped.", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
