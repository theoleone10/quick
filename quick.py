"""Quick CLI.

    python quick.py serve                    run the server (same as python server.py)
    python quick.py init <folder>            starter site + CLAUDE.md so coding agents know the APIs
    python quick.py deploy <folder> [name]   upload a folder; it's live at <name>.<domain>
    python quick.py list                     list sites
    python quick.py delete <name>            delete a site and its data

Env: QUICK_SERVER (default http://localhost:8000), QUICK_SECRET (if the server uses one).
"""

import getpass
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
SERVER = os.environ.get("QUICK_SERVER", "http://localhost:8000").rstrip("/")
SECRET = os.environ.get("QUICK_SECRET")
SKIP = {".git", "node_modules", "__pycache__", ".DS_Store", ".venv"}


def request(method, path, data=None, ctype=None):
    req = urllib.request.Request(SERVER + path, data=data, method=method)
    req.add_header("X-Quick-User", getpass.getuser())
    req.add_header("User-Agent", "quick-cli/1.0")
    if SECRET:
        req.add_header("Authorization", "Bearer " + SECRET)
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except Exception:
            msg = e.reason
        sys.exit(f"error: {msg}")
    except urllib.error.URLError as e:
        sys.exit(f"error: can't reach {SERVER} ({e.reason}). Is `python quick.py serve` running?")


def site_url(name):
    u = urlsplit(SERVER)
    return f"{u.scheme}://{name}.{u.netloc}/"


def deploy(folder, name=None):
    root = Path(folder).resolve()
    if not root.is_dir():
        sys.exit(f"error: {folder} is not a folder")
    name = (name or re.sub(r"[^a-z0-9-]+", "-", root.name.lower())).strip("-")
    if not (root / "index.html").exists():
        print("warning: no index.html at the top of the folder")
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if any(part in SKIP for part in rel.parts) or not p.is_file():
                continue
            zf.write(p, rel.as_posix())
            n += 1
    print(f"uploading {n} files ({buf.tell() / 1024:.0f} KB)...")
    res = request("POST", f"/_quick/api/deploy?site={name}", buf.getvalue(), "application/zip")
    print(f"live: {site_url(res['site'])}")


STARTER = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{name}</title>
  <script src="/_quick/quick.js"></script>
  <style>
    body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 640px; margin: 40px auto; padding: 0 16px; }}
    li {{ padding: 4px 0; }}  small {{ color: #888; }}
  </style>
</head>
<body>
  <h1>Hi, <span id="me">...</span></h1>
  <p>This is <b>{name}</b>. Leave a note; everyone viewing sees it appear instantly.</p>
  <form id="f"><input id="t" placeholder="Say something" required> <button>Post</button></form>
  <ul id="notes"></ul>
  <script>
    const notes = quick.db.collection("notes");
    quick.me().then(me => document.getElementById("me").textContent = me.name);
    notes.watch(docs => {{
      const ul = document.getElementById("notes");
      ul.replaceChildren(...docs.map(d => {{
        const li = document.createElement("li");
        li.textContent = d.text + " ";
        const s = document.createElement("small"); s.textContent = "- " + d.createdBy;
        li.append(s); return li;
      }}));
    }}, {{ order: "-createdAt", limit: 50 }});
    document.getElementById("f").onsubmit = async e => {{
      e.preventDefault();
      const t = document.getElementById("t");
      await notes.create({{ text: t.value }});
      t.value = "";
    }};
  </script>
</body>
</html>
"""


def init(folder):
    root = Path(folder)
    root.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^a-z0-9-]+", "-", root.resolve().name.lower()).strip("-")
    if not (root / "index.html").exists():
        (root / "index.html").write_text(STARTER.format(name=name), encoding="utf-8")
    (root / "CLAUDE.md").write_text((HERE / "AGENT_GUIDE.md").read_text(encoding="utf-8"), encoding="utf-8")
    print(f"created {root / 'index.html'} and {root / 'CLAUDE.md'}")
    print(f"deploy with: python {Path(__file__).name} deploy {folder}")


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    if cmd == "serve":
        sys.path.insert(0, str(HERE))
        import server
        server.main()
    elif cmd == "init" and len(args) == 1:
        init(args[0])
    elif cmd == "deploy" and 1 <= len(args) <= 2:
        deploy(*args)
    elif cmd == "list":
        for s in request("GET", "/_quick/api/sites"):
            print(f"{s['name']:<24} {site_url(s['name'])}  (by {s['deployed_by']})")
    elif cmd == "delete" and len(args) == 1:
        request("DELETE", f"/_quick/api/sites/{args[0]}")
        print(f"deleted {args[0]}")
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
