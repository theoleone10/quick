"""
Quick - a tiny self-hosted take on Shopify's internal "Quick" platform.

Every site is a folder.  <name>.<domain> serves  data/sites/<name>/.
Every site gets the same small, fixed set of backend APIs at /_quick/api/*,
which the browser reaches through /_quick/quick.js:

    db        document collections + realtime subscriptions (SQLite)
    rooms     realtime pub/sub with presence (Server-Sent Events)
    files     uploads
    ai        Claude, with the API key kept on the server
    me        who is viewing

Pure Python standard library (the AI endpoint additionally needs `pip install anthropic`).

Config (environment variables):
    QUICK_PORT     default 8000
    QUICK_HOST     default 127.0.0.1  (use 0.0.0.0 to expose it)
    QUICK_DOMAIN   default localhost  (sites live at <name>.localhost:8000)
    QUICK_DATA     default ./data
    QUICK_SECRET   if set: viewers must log in with it, and deploys must send it
    QUICK_USER     name used for everyone when QUICK_SECRET is unset (default: OS user)
    QUICK_AI_MODEL default claude-opus-5-5

  Going live:
    QUICK_PUBLIC_URL             e.g. https://example.com  (sites at https://<name>.example.com)
    QUICK_TLS_CERT, QUICK_TLS_KEY  serve HTTPS directly (e.g. a Cloudflare origin certificate)
    QUICK_GOOGLE_CLIENT_ID, QUICK_GOOGLE_CLIENT_SECRET   switch login to "Sign in with Google"
    QUICK_ALLOWED_EMAILS         comma-separated emails allowed in
    QUICK_ALLOWED_DOMAINS        comma-separated email domains allowed in (e.g. mycompany.com)
    QUICK_PUBLIC_SITES           comma-separated sites that open without sign-in (db + rooms only)
"""

import base64
import getpass
import hashlib
import hmac
import io
import json
import mimetypes
import os
import queue
import re
import shutil
import sqlite3
import ssl
import sys
import threading
import time
import traceback
import uuid
import zipfile
from collections import defaultdict, deque
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("QUICK_DATA", ROOT / "data")).resolve()
SITES = DATA / "sites"
FILES = DATA / "files"
CLIENT_JS = ROOT / "client" / "quick.js"

PORT = int(os.environ.get("QUICK_PORT", "8000"))
HOST = os.environ.get("QUICK_HOST", "127.0.0.1")
DOMAIN = os.environ.get("QUICK_DOMAIN", "localhost").lower()
SECRET = os.environ.get("QUICK_SECRET") or None
DEFAULT_USER = os.environ.get("QUICK_USER") or getpass.getuser()
AI_MODEL = os.environ.get("QUICK_AI_MODEL", "claude-opus-5-5")

PUBLIC_URL = os.environ.get("QUICK_PUBLIC_URL", "").rstrip("/") or f"http://{DOMAIN}:{PORT}"
SCHEME = urlsplit(PUBLIC_URL).scheme
SECURE = SCHEME == "https"
TLS_CERT = os.environ.get("QUICK_TLS_CERT")
TLS_KEY = os.environ.get("QUICK_TLS_KEY")
GOOGLE_ID = os.environ.get("QUICK_GOOGLE_CLIENT_ID")
GOOGLE_SECRET = os.environ.get("QUICK_GOOGLE_CLIENT_SECRET")
ALLOWED_EMAILS = {e.strip().lower() for e in os.environ.get("QUICK_ALLOWED_EMAILS", "").split(",") if e.strip()}
ALLOWED_DOMAINS = {d.strip().lower().lstrip("@") for d in os.environ.get("QUICK_ALLOWED_DOMAINS", "").split(",") if d.strip()}
AUTH = "google" if GOOGLE_ID else ("password" if SECRET else "none")
# Sites anyone can open without signing in. Guests get the page, quick.js, me, db and rooms;
# AI and file uploads stay behind login so a public link can't run up bills or host files.
PUBLIC_SITES = {s.strip().lower() for s in os.environ.get("QUICK_PUBLIC_SITES", "").split(",") if s.strip()}
GUEST = "guest"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
SESSION_DAYS = 30
# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
FALLBACK_MODELS = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}

MAX_JSON = 1 * 1024 * 1024          # 1 MB per document / request body
MAX_UPLOAD = 50 * 1024 * 1024       # 50 MB per uploaded file
MAX_AI = 30 * 1024 * 1024           # 30 MB per AI request (images / PDFs as base64)
MAX_DEPLOY = 95 * 1024 * 1024       # 95 MB per deploy (zipped); Cloudflare caps bodies at 100 MB
MAX_UNZIPPED = 500 * 1024 * 1024
WRITE_LIMIT = (200, 10.0)           # 200 writes per 10 s per user per site

SITE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
META_KEYS = ("id", "createdAt", "updatedAt", "createdBy")

for ext, typ in {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
                 ".svg": "image/svg+xml", ".json": "application/json", ".wasm": "application/wasm",
                 ".webp": "image/webp", ".md": "text/markdown"}.items():
    mimetypes.add_type(typ, ext)


class HTTPError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status, self.msg = status, msg


# --------------------------------------------------------------------------- storage

class DB:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS docs (
                site TEXT, coll TEXT, id TEXT, data TEXT,
                created_at REAL, updated_at REAL, created_by TEXT,
                PRIMARY KEY (site, coll, id));
            CREATE TABLE IF NOT EXISTS sites (
                name TEXT PRIMARY KEY, deployed_by TEXT, deployed_at REAL, files INTEGER, bytes INTEGER);
            CREATE TABLE IF NOT EXISTS files (
                site TEXT, id TEXT, name TEXT, mime TEXT, size INTEGER,
                uploaded_by TEXT, created_at REAL, PRIMARY KEY (site, id));
        """)

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.rowcount


def doc_out(row):
    return {**json.loads(row["data"]), "id": row["id"], "createdAt": row["created_at"],
            "updatedAt": row["updated_at"], "createdBy": row["created_by"]}


def clean(data):
    if not isinstance(data, dict):
        raise HTTPError(400, "document must be a JSON object")
    return {k: v for k, v in data.items() if k not in META_KEYS}


# --------------------------------------------------------------------------- realtime hub

class Hub:
    """In-memory pub/sub. Every subscriber is a queue fed by publish()."""

    def __init__(self):
        self.lock = threading.Lock()
        self.subs = defaultdict(set)
        self.members = defaultdict(dict)   # room key -> {cid: user}

    def subscribe(self, key):
        q = queue.Queue(maxsize=1000)
        with self.lock:
            self.subs[key].add(q)
        return q

    def unsubscribe(self, key, q):
        with self.lock:
            self.subs[key].discard(q)
            if not self.subs[key]:
                del self.subs[key]

    def publish(self, key, event):
        with self.lock:
            targets = list(self.subs.get(key, ()))
        for q in targets:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # slow consumer; drop rather than block everyone


class RateLimiter:
    def __init__(self, n, window):
        self.n, self.window = n, window
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key):
        now = time.time()
        with self.lock:
            d = self.hits[key]
            while d and now - d[0] > self.window:
                d.popleft()
            if len(d) >= self.n:
                raise HTTPError(429, "slow down: too many writes")
            d.append(now)


SITES.mkdir(parents=True, exist_ok=True)
FILES.mkdir(parents=True, exist_ok=True)
db = DB(DATA / "quick.db")
hub = Hub()
limiter = RateLimiter(*WRITE_LIMIT)
_ai_client = None


# --------------------------------------------------------------------------- auth

def sign(obj):
    payload = base64.urlsafe_b64encode(json.dumps({**obj, "t": int(time.time())}).encode()).decode()
    sig = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify(token, max_age=SESSION_DAYS * 86400):
    try:
        payload, sig = token.rsplit(".", 1)
        good = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        obj = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return obj if time.time() - obj["t"] < max_age else None
    except Exception:
        return None


def cookie(name, value, max_age, shared=True):
    c = f"{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
    if shared and "." in DOMAIN:
        c += f"; Domain={DOMAIN}"   # one login covers every <site>.DOMAIN
    if SECURE:
        c += "; Secure"
    return c


def email_allowed(email):
    email = email.lower()
    if not ALLOWED_EMAILS and not ALLOWED_DOMAINS:
        return False  # fail closed: an allowlist is required
    return email in ALLOWED_EMAILS or email.rsplit("@", 1)[-1] in ALLOWED_DOMAINS


def safe_next(url):
    """Only redirect back to our own domain or its subdomains."""
    try:
        u = urlsplit(url)
        host = (u.hostname or "").lower()
        if u.scheme in ("http", "https") and (host == DOMAIN or host.endswith("." + DOMAIN)):
            return url
    except ValueError:
        pass
    return PUBLIC_URL + "/"


def http_json(url, data=None, headers=None):
    req = Request(url, data=urlencode(data).encode() if data else None, headers=headers or {})
    with urlopen(req, timeout=15) as r:
        return json.loads(r.read())


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "Quick/1.0"
    timeout = 60  # drop idle or stalled connections (SSE keeps itself alive with pings)
    email = None

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.headers.get('Host', '-')} {fmt % args}\n")

    def do_GET(self): self.handle_any("GET")
    def do_POST(self): self.handle_any("POST")
    def do_PUT(self): self.handle_any("PUT")
    def do_PATCH(self): self.handle_any("PATCH")
    def do_DELETE(self): self.handle_any("DELETE")

    # ---- plumbing

    def send_bytes(self, status, body, ctype, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, obj, headers=None):
        self.send_bytes(status, json.dumps(obj).encode(), "application/json", headers)

    def send_html(self, status, html, headers=None):
        self.send_bytes(status, html.encode(), "text/html; charset=utf-8", headers)

    def redirect(self, location, headers=None):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def body(self, limit=MAX_JSON):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise HTTPError(413, f"body too large (max {limit // 1024 // 1024} MB)")
        return self.rfile.read(n) if n else b""

    def json_body(self):
        raw = self.body()
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise HTTPError(400, "invalid JSON")

    # ---- request entry

    def handle_any(self, method):
        try:
            self.route(method)
        except HTTPError as e:
            self.send_json(e.status, {"error": e.msg})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self.send_json(500, {"error": "internal error"})
            except Exception:
                pass

    def site_from_host(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower().strip(".")
        if host.endswith("." + DOMAIN):
            name = host[: -len(DOMAIN) - 1]
            if not SITE_RE.match(name):
                raise HTTPError(404, "no such site")
            return name
        return None  # root host (dashboard, deploy API)

    def cookie_value(self, name):
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return None
        return jar[name].value if name in jar else None

    def current_user(self):
        """Returns the viewer's display name (and sets self.email), or None if not logged in."""
        self.email = None
        if AUTH == "none":
            return DEFAULT_USER
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].encode(), SECRET.encode()):
            return self.headers.get("X-Quick-User") or "cli"
        token = self.cookie_value("quick_session")
        session = verify(token) if token else None
        if not session:
            return None
        self.email = session.get("e")
        return session.get("n")

    @staticmethod
    def guest_allowed(method, path):
        if not path.startswith("/_quick/"):
            return method == "GET"  # the site's static files
        if path == "/_quick/quick.js" or path == "/_quick/api/me":
            return True
        return path.startswith(("/_quick/api/db/", "/_quick/api/rooms/"))

    def route(self, method):
        url = urlsplit(self.path)
        path = unquote(url.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        site = self.site_from_host()

        if path == "/_quick/login":
            return self.login(method, query)
        if path == "/_quick/auth/google":
            return self.google_start(query)
        if path == "/_quick/auth/callback":
            return self.google_callback(query)
        if path == "/_quick/logout":
            return self.redirect(PUBLIC_URL + "/", {"Set-Cookie": cookie("quick_session", "", 0)})

        # Block other origins (including other Quick sites) from writing through this site's API.
        if method != "GET" and path.startswith("/_quick/api/"):
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
                raise HTTPError(403, "cross-origin write blocked")

        user = self.current_user()
        if user is None and site in PUBLIC_SITES and self.guest_allowed(method, path):
            user = GUEST
        if user is None:
            if path.startswith("/_quick/api/"):
                raise HTTPError(401, "not logged in")
            if AUTH == "google":
                here = f"{SCHEME}://{self.headers.get('Host', DOMAIN)}{self.path}"
                return self.redirect(f"{PUBLIC_URL}/_quick/auth/google?next={quote(here, safe='')}")
            return self.redirect("/_quick/login?next=" + quote(self.path))

        if path == "/_quick/quick.js":
            return self.send_bytes(200, CLIENT_JS.read_bytes(), "text/javascript",
                                   {"Cache-Control": "no-cache"})
        if path.startswith("/_quick/files/"):
            return self.serve_upload(site, path)
        if path.startswith("/_quick/api/"):
            return self.api(method, path[len("/_quick/api/"):].strip("/"), query, site, user)
        if method != "GET":
            raise HTTPError(405, "method not allowed")
        if site is None:
            return self.send_html(200, DASHBOARD)
        return self.serve_static(site, path)

    # ---- login (only when QUICK_SECRET is set)

    def login(self, method, query):
        nxt = query.get("next", "/")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        if AUTH != "password":
            return self.redirect(nxt)
        error = ""
        if method == "POST":
            form = {k: v[-1] for k, v in parse_qs(self.body().decode()).items()}
            name = (form.get("name") or "").strip()[:60]
            if name and hmac.compare_digest(form.get("password", "").encode(), SECRET.encode()):
                return self.redirect(nxt, {"Set-Cookie": cookie("quick_session", sign({"n": name}),
                                                                SESSION_DAYS * 86400)})
            error = "<p class=err>Wrong password (or missing name).</p>"
        self.send_html(200, LOGIN_PAGE.replace("{{error}}", error).replace("{{next}}", quote(nxt)))

    # ---- Google sign-in (only when QUICK_GOOGLE_CLIENT_ID is set)

    def google_start(self, query):
        if AUTH != "google":
            raise HTTPError(404, "Google sign-in is not configured")
        nonce = uuid.uuid4().hex
        params = {"client_id": GOOGLE_ID, "redirect_uri": PUBLIC_URL + "/_quick/auth/callback",
                  "response_type": "code", "scope": "openid email profile", "state": nonce,
                  "prompt": "select_account"}
        if len(ALLOWED_DOMAINS) == 1 and not ALLOWED_EMAILS:
            params["hd"] = next(iter(ALLOWED_DOMAINS))  # hint Google's account picker
        state = sign({"s": nonce, "next": safe_next(query.get("next", ""))})
        self.redirect(f"{GOOGLE_AUTH_URL}?{urlencode(params)}",
                      {"Set-Cookie": cookie("quick_oauth", state, 600, shared=False)})

    def google_callback(self, query):
        if AUTH != "google":
            raise HTTPError(404, "Google sign-in is not configured")
        token = self.cookie_value("quick_oauth")
        state = verify(token, max_age=600) if token else None
        if not state or not hmac.compare_digest(state["s"], query.get("state", "")):
            return self.send_html(400, MESSAGE_PAGE.format(
                title="Sign-in expired", body=f'<a href="{PUBLIC_URL}/">Try again</a>'))
        if "code" not in query:
            return self.send_html(400, MESSAGE_PAGE.format(title="Sign-in cancelled", body=""))
        try:
            tokens = http_json(GOOGLE_TOKEN_URL, {
                "code": query["code"], "client_id": GOOGLE_ID, "client_secret": GOOGLE_SECRET,
                "redirect_uri": PUBLIC_URL + "/_quick/auth/callback", "grant_type": "authorization_code"})
            info = http_json(GOOGLE_USERINFO_URL,
                             headers={"Authorization": "Bearer " + tokens["access_token"]})
        except Exception as e:
            sys.stderr.write(f"google sign-in failed: {e}\n")
            raise HTTPError(502, "Google sign-in failed, try again")
        email = (info.get("email") or "").lower()
        if not info.get("email_verified") or not email_allowed(email):
            return self.send_html(403, MESSAGE_PAGE.format(
                title="Not on the list",
                body=f"<p>{html_escape(email)} isn't allowed into this Quick.</p>"
                     f'<p><a href="{PUBLIC_URL}/_quick/logout">Use a different account</a></p>'))
        name = info.get("name") or email.split("@")[0]
        self.send_response(302)
        self.send_header("Location", state["next"])
        self.send_header("Set-Cookie", cookie("quick_session", sign({"n": name, "e": email}),
                                              SESSION_DAYS * 86400))
        self.send_header("Set-Cookie", cookie("quick_oauth", "", 0, shared=False))
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- static sites

    def serve_static(self, site, path):
        base = (SITES / site).resolve()
        if not base.is_dir():
            return self.send_html(404, NOT_DEPLOYED.replace("{{site}}", site))
        target = (base / path.lstrip("/")).resolve()
        if target != base and base not in target.parents:
            raise HTTPError(404, "not found")
        if target.is_dir():
            if not path.endswith("/"):
                return self.redirect(path + "/")
            target = target / "index.html"
        elif not target.exists() and not target.suffix and target.with_name(target.name + ".html").is_file():
            target = target.with_name(target.name + ".html")
        if not target.is_file():
            page = base / "404.html"
            if page.is_file():
                return self.send_bytes(404, page.read_bytes(), "text/html; charset=utf-8")
            return self.send_html(404, "<h1>404</h1><p>Not found.</p>")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") and "charset" not in ctype:
            ctype += "; charset=utf-8"
        self.send_bytes(200, target.read_bytes(), ctype, {"Cache-Control": "no-cache"})

    def serve_upload(self, site, path):
        if site is None:
            raise HTTPError(404, "not found")
        parts = path[len("/_quick/files/"):].split("/")
        rows = db.q("SELECT * FROM files WHERE site=? AND id=?", (site, parts[0]))
        if not rows:
            raise HTTPError(404, "no such file")
        f = rows[0]
        data = (FILES / site / f["id"]).read_bytes()
        self.send_bytes(200, data, f["mime"], {
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(f['name'])}",
            "X-Content-Type-Options": "nosniff"})

    # ---- API

    def api(self, method, rest, query, site, user):
        parts = rest.split("/") if rest else []
        head = parts[0] if parts else ""

        if head == "me" and method == "GET":
            return self.send_json(200, {"name": user, "email": self.email, "site": site})

        if site is None:  # root-host APIs: site management
            if head == "sites" and len(parts) == 1 and method == "GET":
                return self.send_json(200, db.q("SELECT * FROM sites ORDER BY deployed_at DESC"))
            if head == "deploy" and method == "POST":
                return self.deploy(query.get("site", ""), user)
            if head == "sites" and len(parts) == 2 and method == "DELETE":
                return self.delete_site(parts[1])
            raise HTTPError(404, "unknown endpoint (site APIs live on <site>." + DOMAIN + ")")

        if method != "GET":
            limiter.check((user, site))

        if head == "db" and len(parts) >= 2:
            coll = parts[1]
            if not NAME_RE.match(coll):
                raise HTTPError(400, "bad collection name")
            if len(parts) == 2:
                if method == "GET":
                    return self.db_list(site, coll, query)
                if method == "POST":
                    return self.db_write(site, coll, uuid.uuid4().hex[:16], "create", user)
            elif len(parts) == 3 and parts[2] == "events" and method == "GET":
                return self.stream(f"db:{site}:{coll}")
            elif len(parts) == 3:
                doc_id = parts[2]
                if not NAME_RE.match(doc_id):
                    raise HTTPError(400, "bad id")
                if method == "GET":
                    rows = db.q("SELECT * FROM docs WHERE site=? AND coll=? AND id=?", (site, coll, doc_id))
                    if not rows:
                        raise HTTPError(404, "not found")
                    return self.send_json(200, doc_out(rows[0]))
                if method == "PUT":
                    return self.db_write(site, coll, doc_id, "set", user)
                if method == "PATCH":
                    return self.db_write(site, coll, doc_id, "update", user)
                if method == "DELETE":
                    n = db.x("DELETE FROM docs WHERE site=? AND coll=? AND id=?", (site, coll, doc_id))
                    if n:
                        hub.publish(f"db:{site}:{coll}", {"type": "delete", "id": doc_id})
                    return self.send_json(200, {"deleted": bool(n)})

        if head == "rooms" and len(parts) >= 2 and NAME_RE.match(parts[1]):
            key = f"room:{site}:{parts[1]}"
            if len(parts) == 3 and parts[2] == "events" and method == "GET":
                return self.room_stream(key, query.get("cid") or uuid.uuid4().hex, user)
            if len(parts) == 2 and method == "POST":
                body = self.json_body()
                hub.publish(key, {"type": "message", "user": user, "from": body.get("cid"),
                                  "data": body.get("data"), "at": time.time()})
                return self.send_json(200, {"ok": True})
            if len(parts) == 2 and method == "GET":
                return self.send_json(200, {"members": list(hub.members.get(key, {}).values())})

        if head == "files":
            if len(parts) == 1 and method == "POST":
                return self.upload(site, user)
            if len(parts) == 1 and method == "GET":
                rows = db.q("SELECT * FROM files WHERE site=? ORDER BY created_at DESC", (site,))
                return self.send_json(200, [file_out(r) for r in rows])
            if len(parts) == 2 and method == "DELETE":
                n = db.x("DELETE FROM files WHERE site=? AND id=?", (site, parts[1]))
                if n:
                    (FILES / site / parts[1]).unlink(missing_ok=True)
                return self.send_json(200, {"deleted": bool(n)})

        if head == "ai" and method == "POST":
            try:
                body = json.loads(self.body(MAX_AI) or b"{}")
            except ValueError:
                raise HTTPError(400, "invalid JSON")
            if body.get("stream"):
                return self.ai_stream(body)
            return self.send_json(200, ask_claude(body))

        raise HTTPError(404, "unknown endpoint")

    # ---- db

    def db_list(self, site, coll, query):
        rows = [doc_out(r) for r in db.q("SELECT * FROM docs WHERE site=? AND coll=?", (site, coll))]
        if query.get("where"):
            try:
                where = json.loads(query["where"])
            except ValueError:
                raise HTTPError(400, "where must be JSON")
            rows = [r for r in rows if all(r.get(k) == v for k, v in where.items())]
        order = query.get("order", "createdAt")
        desc = order.startswith("-")
        field = order.lstrip("-")
        rows.sort(key=lambda r: (r.get(field) is None, str(type(r.get(field))), r.get(field) or 0), reverse=desc)
        limit = int(query.get("limit", 1000))
        return self.send_json(200, rows[:limit])

    def db_write(self, site, coll, doc_id, mode, user):
        data = clean(self.json_body())
        now = time.time()
        key = f"db:{site}:{coll}"
        existing = db.q("SELECT * FROM docs WHERE site=? AND coll=? AND id=?", (site, coll, doc_id))
        if mode == "update":
            if not existing:
                raise HTTPError(404, "not found")
            data = {**json.loads(existing[0]["data"]), **data}
        if existing:
            db.x("UPDATE docs SET data=?, updated_at=? WHERE site=? AND coll=? AND id=?",
                 (json.dumps(data), now, site, coll, doc_id))
            event = "update"
        else:
            db.x("INSERT INTO docs VALUES (?,?,?,?,?,?,?)",
                 (site, coll, doc_id, json.dumps(data), now, now, user))
            event = "create"
        doc = doc_out(db.q("SELECT * FROM docs WHERE site=? AND coll=? AND id=?", (site, coll, doc_id))[0])
        hub.publish(key, {"type": event, "doc": doc})
        self.send_json(201 if event == "create" else 200, doc)

    # ---- realtime (Server-Sent Events)

    def open_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def emit(self, event):
        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode() if event else b": ping\n\n")
        self.wfile.flush()

    def pump(self, q):
        while True:
            try:
                self.emit(q.get(timeout=15))
            except queue.Empty:
                self.emit(None)  # keepalive; also how we notice a closed tab

    def stream(self, key):
        q = hub.subscribe(key)
        try:
            self.open_stream()
            self.emit({"type": "ready"})
            self.pump(q)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            hub.unsubscribe(key, q)

    def room_stream(self, key, cid, user):
        q = hub.subscribe(key)
        with hub.lock:
            hub.members[key][cid] = {"cid": cid, "user": user}
            members = list(hub.members[key].values())
        try:
            self.open_stream()
            self.emit({"type": "hello", "cid": cid, "user": user, "members": members})
            hub.publish(key, {"type": "join", "cid": cid, "user": user})
            self.pump(q)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            hub.unsubscribe(key, q)
            with hub.lock:
                hub.members[key].pop(cid, None)
                if not hub.members[key]:
                    del hub.members[key]
            hub.publish(key, {"type": "leave", "cid": cid, "user": user})

    def ai_stream(self, body):
        """Stream Claude's answer as SSE over the POST response.
        Events: {"type":"text","text":...} ... then {"type":"done",...} or {"type":"error",...}.
        A worker thread drives the API stream so we can send keepalives during long silences
        (e.g. while the model thinks), keeping Cloudflare's 100 s idle limit from cutting us off."""
        anthropic, client, kwargs = claude_request(body)
        q = queue.Queue()

        def worker():
            try:
                with client.beta.messages.stream(**kwargs) as stream:
                    for text in stream.text_stream:
                        q.put({"type": "text", "text": text})
                    final = stream.get_final_message()
                q.put({"type": "done", "stop_reason": final.stop_reason, "model": final.model})
            except anthropic.RateLimitError:
                q.put({"type": "error", "error": "Claude rate limit hit, try again shortly"})
            except anthropic.APIStatusError as e:
                q.put({"type": "error", "error": f"Claude API error {e.status_code}: {e.message}"})
            except anthropic.APIConnectionError:
                q.put({"type": "error", "error": "could not reach the Claude API"})
            except Exception as e:
                traceback.print_exc()
                q.put({"type": "error", "error": f"AI failed: {type(e).__name__}"})

        threading.Thread(target=worker, daemon=True).start()
        try:
            self.open_stream()
            while True:
                try:
                    ev = q.get(timeout=10)
                except queue.Empty:
                    self.emit(None)
                    continue
                self.emit(ev)
                if ev["type"] in ("done", "error"):
                    break
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    # ---- uploads

    def upload(self, site, user):
        name = unquote(self.headers.get("X-Filename") or "file")[:200].replace("/", "_").replace("\\", "_")
        data = self.body(MAX_UPLOAD)
        fid = uuid.uuid4().hex
        mime = self.headers.get("Content-Type") or mimetypes.guess_type(name)[0] or "application/octet-stream"
        (FILES / site).mkdir(parents=True, exist_ok=True)
        (FILES / site / fid).write_bytes(data)
        db.x("INSERT INTO files VALUES (?,?,?,?,?,?,?)", (site, fid, name, mime, len(data), user, time.time()))
        self.send_json(201, file_out(db.q("SELECT * FROM files WHERE site=? AND id=?", (site, fid))[0]))

    # ---- deploys

    def deploy(self, site, user):
        if not SITE_RE.match(site):
            raise HTTPError(400, "site names: lowercase letters, digits and dashes")
        try:
            zf = zipfile.ZipFile(io.BytesIO(self.body(MAX_DEPLOY)))
        except zipfile.BadZipFile:
            raise HTTPError(400, "body must be a zip file")
        tmp = SITES / f".tmp-{site}-{uuid.uuid4().hex[:8]}"
        count = total = 0
        try:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if info.is_dir():
                    continue
                parts = [p for p in name.split("/") if p]
                if not parts or name.startswith("/") or ".." in parts or ":" in parts[0]:
                    raise HTTPError(400, f"unsafe path in zip: {name}")
                total += info.file_size
                if total > MAX_UNZIPPED:
                    raise HTTPError(413, "site too large")
                dest = tmp.joinpath(*parts)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                count += 1
            tmp.mkdir(exist_ok=True)
            final = SITES / site
            old = None
            if final.exists():
                old = SITES / f".old-{site}-{uuid.uuid4().hex[:8]}"
                final.rename(old)
            tmp.rename(final)
            if old:
                shutil.rmtree(old, ignore_errors=True)
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
        db.x("INSERT OR REPLACE INTO sites VALUES (?,?,?,?,?)", (site, user, time.time(), count, total))
        self.send_json(200, {"site": site, "files": count, "bytes": total})

    def delete_site(self, site):
        if not SITE_RE.match(site):
            raise HTTPError(400, "bad site name")
        shutil.rmtree(SITES / site, ignore_errors=True)
        db.x("DELETE FROM sites WHERE name=?", (site,))
        db.x("DELETE FROM docs WHERE site=?", (site,))
        db.x("DELETE FROM files WHERE site=?", (site,))
        shutil.rmtree(FILES / site, ignore_errors=True)
        self.send_json(200, {"deleted": site})


def file_out(r):
    return {"id": r["id"], "name": r["name"], "mime": r["mime"], "size": r["size"],
            "uploadedBy": r["uploaded_by"], "createdAt": r["created_at"],
            "url": f"/_quick/files/{r['id']}/{quote(r['name'])}"}


# --------------------------------------------------------------------------- AI

def claude_request(body):
    """Validate a /ai request body and build Messages API kwargs. Returns (anthropic module, client, kwargs)."""
    global _ai_client
    try:
        import anthropic
    except ImportError:
        raise HTTPError(501, "AI is off: run `pip install anthropic` and set ANTHROPIC_API_KEY, then restart")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise HTTPError(501, "AI is off: set ANTHROPIC_API_KEY on the server")
    if _ai_client is None:
        _ai_client = anthropic.Anthropic()

    messages = body.get("messages")
    if not messages:
        prompt = body.get("prompt")
        if not prompt:
            raise HTTPError(400, "send `prompt` or `messages`")
        messages = [{"role": "user", "content": str(prompt)}]
    if not isinstance(messages, list):
        raise HTTPError(400, "`messages` must be a list")

    model = body.get("model") or AI_MODEL
    kwargs = {"model": model, "max_tokens": max(1, min(int(body.get("max_tokens") or 16000), 32000)),
              "messages": messages}
    if body.get("system"):
        kwargs["system"] = str(body["system"])
    output_config = {}
    oc = body.get("output_config") or {}
    if isinstance(oc, dict):
        if isinstance(oc.get("format"), dict):
            output_config["format"] = oc["format"]          # structured outputs (json_schema)
        if oc.get("effort") in ("low", "medium", "high", "xhigh", "max"):
            output_config["effort"] = oc["effort"]
    if output_config:
        kwargs["output_config"] = output_config
    if model in FALLBACK_MODELS:
        # If the model declines on policy grounds, the API retries on a suitable fallback model.
        kwargs["betas"] = ["server-side-fallback-2026-07-01"]
        kwargs["fallbacks"] = "default"
    return anthropic, _ai_client, kwargs


def ask_claude(body):
    anthropic, client, kwargs = claude_request(body)
    try:
        resp = client.beta.messages.create(**kwargs)
    except anthropic.RateLimitError:
        raise HTTPError(429, "Claude rate limit hit, try again shortly")
    except anthropic.APIStatusError as e:
        raise HTTPError(502, f"Claude API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise HTTPError(502, "could not reach the Claude API")
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return {"text": text, "stop_reason": resp.stop_reason, "model": resp.model}


# --------------------------------------------------------------------------- pages

DASHBOARD = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Quick</title>
<style>
:root{--bg:#fafaf9;--fg:#1c1917;--mut:#78716c;--card:#fff;--line:#e7e5e4;--acc:#16a34a}
@media (prefers-color-scheme:dark){:root{--bg:#0c0a09;--fg:#f5f5f4;--mut:#a8a29e;--card:#1c1917;--line:#292524;--acc:#4ade80}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:40px 16px}
h1{font-size:28px;margin:0}h1 span{color:var(--acc)}p{color:var(--mut)}
code,pre{font:13px ui-monospace,monospace}pre{background:var(--card);border:1px solid var(--line);padding:12px;border-radius:8px;overflow-x:auto}
ul{list-style:none;padding:0}li{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:8px 0;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
a{color:var(--fg);font-weight:600}small{color:var(--mut)}
</style></head><body><main>
<h1><span>&#9889;</span> Quick</h1>
<p>Drop a folder, get a site. Every site gets a database, realtime rooms, file uploads, AI, and identity.</p>
<pre>python quick.py init my-site     # starter page + agent instructions
python quick.py deploy my-site   # live at my-site.<span id=host></span></pre>
<h2>Sites</h2><ul id=list><li><small>Loading&hellip;</small></li></ul>
</main><script>
const H=location.host;document.getElementById('host').textContent=H;
fetch('/_quick/api/sites').then(r=>r.json()).then(s=>{const ul=document.getElementById('list');
 ul.innerHTML=s.length?'':'<li><small>No sites yet.</small></li>';
 for(const x of s){const li=document.createElement('li');const a=document.createElement('a');
  a.href=location.protocol+'//'+x.name+'.'+H+'/';a.textContent=x.name;const m=document.createElement('small');
  m.textContent='by '+x.deployed_by+' \\u00b7 '+new Date(x.deployed_at*1000).toLocaleString()+' \\u00b7 '+x.files+' files';
  li.append(a,m);ul.append(li)}});
</script></body></html>"""

LOGIN_PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Quick login</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0c0a09;color:#f5f5f4;font:15px system-ui,sans-serif}
form{display:grid;gap:10px;width:min(320px,90vw)}input,button{font:inherit;padding:10px;border-radius:8px;border:1px solid #44403c;background:#1c1917;color:inherit}
button{background:#16a34a;border:0;font-weight:600;cursor:pointer}.err{color:#f87171;margin:0}</style></head><body>
<form method=post action="/_quick/login?next={{next}}"><h2>&#9889; Quick</h2>{{error}}
<input name=name placeholder="Your name" required autofocus><input name=password type=password placeholder="Password" required>
<button>Enter</button></form></body></html>"""

MESSAGE_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{title}</title><body style="font:16px system-ui;padding:40px 16px;max-width:600px;margin:auto">
<h1>&#9889; {title}</h1>{body}</body>"""


def html_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


NOT_DEPLOYED = """<!doctype html><meta charset=utf-8><title>Not deployed</title>
<body style="font:16px system-ui;padding:40px;max-width:600px;margin:auto">
<h1>&#9889; {{site}} isn't deployed yet</h1>
<p>Claim it with <code>python quick.py deploy &lt;folder&gt; {{site}}</code></p></body>"""


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionError, TimeoutError, OSError)):
            return  # bad TLS handshakes, scanners, dropped connections: not worth a traceback
        super().handle_error(request, client_address)


def main():
    if AUTH == "google":
        if not (SECRET and GOOGLE_SECRET):
            sys.exit("Google sign-in needs QUICK_SECRET (signs sessions) and QUICK_GOOGLE_CLIENT_SECRET")
        if not (ALLOWED_EMAILS or ALLOWED_DOMAINS):
            sys.exit("Google sign-in needs QUICK_ALLOWED_EMAILS and/or QUICK_ALLOWED_DOMAINS")
    server = Server((HOST, PORT), Handler)
    if TLS_CERT:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(TLS_CERT, TLS_KEY)
        # Handshake lazily in the worker thread, so one slow client can't block accept().
        server.socket = ctx.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    who = {"none": "off, everyone is " + DEFAULT_USER, "password": "shared password",
           "google": f"Google ({', '.join(sorted(ALLOWED_EMAILS | {'@' + d for d in ALLOWED_DOMAINS}))})"}[AUTH]
    print(f"Quick on {PUBLIC_URL}/  (sites at <name>.{DOMAIN})  listening {HOST}:{PORT}"
          f"{' with TLS' if TLS_CERT else ''}", flush=True)
    print(f"data: {DATA}  |  auth: {who}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
