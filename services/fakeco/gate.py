"""The demo gate: one username and password in front of the web app, for every kind of client (browser, Android, iOS, curl).

It runs as a sidecar of the `web` pod and uses only the Python standard library. nginx asks it, with `auth_request`, whether a request may
pass (`GET /_verify`); everything except /otlp/ and /healthz asks (web/nginx/default.conf.template). Ask for a token with the same two
calls everywhere:

    POST /gate/login   {"username": "...", "password": "..."}  ->  {"token", "expires_at", "username"}  and a cookie `fakeco_gate`
    any request        Cookie: fakeco_gate=<token>   or   X-Gate-Token: <token>

(Not `Authorization`: the app's own sign-in already uses `Authorization: Bearer` on /api.) A browser fills in the form at /gate/login.

Credentials are lines `username:hash` in a file (default /etc/gate/users, the key `users` of the Secret `web-gate`); the file is read again when
it changes, so a new user works within about a minute (the time Kubernetes needs to update the mounted Secret). `scripts/gate-users.sh` edits it:

    python -m fakeco.gate users FILE list | add USER | passwd USER | remove USER      (password: $GATE_PASSWORD, else made up and printed)
    python -m fakeco.gate serve

A token is `user.expiry.signature`; the signature key is the server secret plus that user's password hash, so changing a password or removing a
user signs them out at once. Settings (environment): FAKECO_GATE_ENABLED (on/off, default on; off lets everything pass), FAKECO_GATE_SESSION_HOURS
(12), FAKECO_GATE_USERS_FILE, FAKECO_GATE_SECRET_FILE (without one, a random secret per start), FAKECO_GATE_BIND (127.0.0.1), FAKECO_GATE_PORT (8090).
With no users file, or an empty one, nobody passes.
"""
import base64, hashlib, hmac, html, json, os, re, secrets, sys, threading, time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

COOKIE = "fakeco_gate"
ITERATIONS = 600_000
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._@-]{0,63}$")
MAX_FAILURES, FAILURE_WINDOW_S = 10, 60


# ---------------------------------------------------------------- users file

def hash_password(password, iterations=ITERATIONS):
    salt = secrets.token_bytes(16)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations).hex()}"


def check_password(password, stored):
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(got, bytes.fromhex(digest))
    except ValueError:
        return False


def parse_users(text):
    """{username: stored hash} from the lines `username:hash`; blank lines and lines starting with # are skipped."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and ":" in line:
            name, stored = line.split(":", 1)
            out[name.strip().lower()] = stored.strip()
    return out


def format_users(users):
    return "# username:hash, one per line. Edit with scripts/gate-users.sh, not by hand.\n" + "".join(f"{n}:{h}\n" for n, h in sorted(users.items()))


def users_command(path, argv):
    """python -m fakeco.gate users FILE list | add USER | passwd USER | remove USER. Writes FILE; prints what it did, and a made-up password once."""
    if len(argv) < 1 or argv[0] not in ("list", "add", "passwd", "remove") or (argv[0] != "list" and len(argv) != 2):
        sys.exit("usage: python -m fakeco.gate users FILE list | add USER | passwd USER | remove USER")
    try:
        users = parse_users(open(path).read())
    except FileNotFoundError:
        users = {}
    action = argv[0]
    if action == "list":
        print("\n".join(sorted(users)) or "(no users: nobody can get in)")
        return
    name = argv[1].strip().lower()
    if action == "remove":
        if name not in users:
            sys.exit(f"no user {name}")
        del users[name]
        print(f"removed {name}")
    else:
        if not USERNAME.match(name):
            sys.exit("a username is 1 to 64 letters, digits, . _ @ -, starting with a letter or digit")
        if action == "add" and name in users:
            sys.exit(f"{name} exists; use passwd to change the password")
        if action == "passwd" and name not in users:
            sys.exit(f"no user {name}")
        password = os.environ.get("GATE_PASSWORD") or ""
        made_up = not password
        if made_up:
            password = secrets.token_urlsafe(9)
        users[name] = hash_password(password)
        print(f"{'added' if action == 'add' else 'changed the password of'} {name}" + (f"; password: {password}" if made_up else ""))
    with open(path, "w") as f:
        f.write(format_users(users))


# ---------------------------------------------------------------- state

class Gate:
    def __init__(self, env=os.environ):
        self.enabled = env.get("FAKECO_GATE_ENABLED", "on").strip().lower() not in ("off", "false", "0", "no")
        self.session_s = int(float(env.get("FAKECO_GATE_SESSION_HOURS", "12")) * 3600)
        self.users_file = env.get("FAKECO_GATE_USERS_FILE", "/etc/gate/users")
        secret_file = env.get("FAKECO_GATE_SECRET_FILE", "/etc/gate/secret")
        self.secret = secrets.token_bytes(32)
        try:
            self.secret = open(secret_file, "rb").read().strip() or self.secret
        except OSError:
            pass
        self._users, self._users_stamp = {}, None
        self._failures = {}
        self._lock = threading.Lock()

    def users(self):
        try:
            st = os.stat(self.users_file)
            stamp = (st.st_mtime_ns, st.st_size)
            if stamp != self._users_stamp:
                self._users, self._users_stamp = parse_users(open(self.users_file).read()), stamp
        except OSError:
            self._users, self._users_stamp = {}, None
        return self._users

    def _sign(self, user, expiry, stored):
        return hmac.new(self.secret + b"\0" + stored.encode(), f"{user}.{expiry}".encode(), hashlib.sha256).hexdigest()

    def issue(self, user):
        expiry = int(time.time()) + self.session_s
        name = base64.urlsafe_b64encode(user.encode()).decode().rstrip("=")
        return f"{name}.{expiry}.{self._sign(user, expiry, self.users()[user])}", expiry

    def verify(self, token):
        """The username a valid token belongs to, or None."""
        try:
            name, expiry, sig = token.split(".")
            user = base64.urlsafe_b64decode(name + "=" * (-len(name) % 4)).decode()
            stored = self.users().get(user)
            if stored and int(expiry) > time.time() and hmac.compare_digest(sig, self._sign(user, int(expiry), stored)):
                return user
        except ValueError:
            pass
        return None

    def login(self, username, password, client):
        """(token, expiry) or an (http status, message). Failures cost half a second, and a client with 10 in a minute waits."""
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._failures.get(client, []) if now - t < FAILURE_WINDOW_S]
            self._failures[client] = recent
            if len(recent) >= MAX_FAILURES:
                return 429, "Too many wrong passwords. Wait a minute."
        user = (username or "").strip().lower()
        stored = self.users().get(user)
        # a user that does not exist costs the same time as a wrong password
        ok = check_password(password or "", stored or hash_password("x", 1000))
        if stored and ok:
            return self.issue(user)
        time.sleep(0.5)
        with self._lock:
            self._failures[client].append(time.monotonic())
        return 401, "Wrong username or password."


# ---------------------------------------------------------------- http

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>FakeCo Securities: demo access</title>
<style>
:root{color-scheme:light dark;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:Canvas;color:CanvasText}
form{width:min(22rem,90vw);display:grid;gap:.9rem;padding:1.6rem;border:1px solid color-mix(in srgb,CanvasText 20%,transparent);border-radius:.75rem}
h1{font-size:1.15rem;margin:0}p{margin:0;font-size:.9rem;opacity:.75}
label{display:grid;gap:.3rem;font-size:.85rem}
input{font:inherit;padding:.55rem .65rem;border:1px solid color-mix(in srgb,CanvasText 30%,transparent);border-radius:.4rem;background:Field;color:FieldText}
button{font:inherit;padding:.6rem;border:0;border-radius:.4rem;background:#1f6feb;color:#fff;cursor:pointer}
.err{color:#d1242f;opacity:1}
</style></head><body>
<form method="post" action="/gate/login">
<h1>FakeCo Securities</h1><p>This demo is private. Sign in to continue.</p>@ERROR@
<input type="hidden" name="next" value="@NEXT@">
<label>Username<input name="username" value="@USER@" autocomplete="username" autocapitalize="none" autofocus required></label>
<label>Password<input name="password" type="password" autocomplete="current-password" required></label>
<button type="submit">Sign in</button>
</form></body></html>"""


def safe_next(value):
    """Only a path on this site; anything else (another host, a gate page) becomes /."""
    if not value or not value.startswith("/") or value.startswith(("//", "/\\", "/gate")) or any(c in value for c in "\r\n\\"):
        return "/"
    return value


def make_handler(gate):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "fakeco-gate"

        def log_message(self, fmt, *args):  # no access log: it would hold paths and tokens
            pass

        def send(self, status, body=b"", ctype="text/plain; charset=utf-8", headers=()):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def json(self, status, data, headers=()):
            self.send(status, json.dumps(data).encode(), "application/json", headers)

        def token(self):
            c = cookies.SimpleCookie()
            try:
                c.load(self.headers.get("Cookie", ""))
            except cookies.CookieError:
                pass
            return self.headers.get("X-Gate-Token") or (c[COOKIE].value if COOKIE in c else "")

        def cookie(self, value, max_age):
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
            return ("Set-Cookie", f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax{secure}")

        def client(self):
            return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

        def form(self, status=200, error="", next_path="/", username=""):
            msg = f'<p class="err">{html.escape(error)}</p>' if error else ""
            self.send(status, PAGE.replace("@ERROR@", msg).replace("@NEXT@", html.escape(next_path, quote=True)).replace("@USER@", html.escape(username, quote=True)).encode(), "text/html; charset=utf-8")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/_verify":  # nginx auth_request: 204 lets the request through
                if not gate.enabled:
                    return self.send(204)
                user = gate.verify(self.token())
                return self.send(204, headers=[("X-Gate-User", user)]) if user else self.send(401)
            if url.path == "/gate/login":
                return self.form(next_path=safe_next(parse_qs(url.query).get("next", [""])[0]))
            if url.path == "/gate/logout":
                return self.send(303, headers=[self.cookie("", 0), ("Location", "/gate/login")])
            if url.path == "/gate/whoami":
                user = gate.verify(self.token())
                return self.json(200 if user else 401, {"username": user, "enabled": gate.enabled})
            self.send(404)

        def do_POST(self):
            url = urlsplit(self.path)
            if url.path == "/gate/logout":
                return self.send(204, headers=[self.cookie("", 0)])
            if url.path != "/gate/login":
                return self.send(404)
            body = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 8192))
            as_json = "json" in self.headers.get("Content-Type", "")
            try:
                data = json.loads(body or b"{}") if as_json else {k: v[0] for k, v in parse_qs(body.decode()).items()}
                username, password = str(data.get("username", "")), str(data.get("password", ""))
            except (ValueError, AttributeError):
                data, username, password = {}, "", ""
            result = gate.login(username, password, self.client())
            if isinstance(result[0], str):
                token, expiry = result
                print(f"gate: {username.strip().lower()} signed in", flush=True)
                headers = [self.cookie(token, gate.session_s)]
                if as_json:
                    return self.json(200, {"token": token, "expires_at": expiry, "username": username.strip().lower()}, headers)
                return self.send(303, headers=headers + [("Location", safe_next(data.get("next")))])
            status, message = result
            print(f"gate: sign-in failed for {username.strip().lower()[:64]!r} from {self.client()}", flush=True)
            if as_json:
                return self.json(status, {"error": {"code": "gate_login_failed", "message": message}})
            self.form(status, message, safe_next(data.get("next")), username)

    return Handler


def serve(env=os.environ):
    gate = Gate(env)
    server = ThreadingHTTPServer((env.get("FAKECO_GATE_BIND", "127.0.0.1"), int(env.get("FAKECO_GATE_PORT", "8090"))), make_handler(gate))
    server.daemon_threads = True
    print(f"gate: {'on' if gate.enabled else 'OFF (everything passes)'}, {len(gate.users())} users, sessions last {gate.session_s // 3600} h, "
          f"listening on {server.server_address[0]}:{server.server_address[1]}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    if sys.argv[1:2] == ["serve"]:
        serve()
    elif sys.argv[1:2] == ["users"] and len(sys.argv) >= 3:
        users_command(sys.argv[2], sys.argv[3:])
    else:
        sys.exit("usage: python -m fakeco.gate serve | users FILE list|add USER|passwd USER|remove USER")
