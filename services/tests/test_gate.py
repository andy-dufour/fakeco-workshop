"""The demo gate (fakeco/gate.py): sign-in over HTTP, tokens, the users file and its command line."""
import json, threading, time, types, urllib.error, urllib.parse, urllib.request

import pytest

from fakeco import gate


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def call(base, path, method="GET", data=None, headers=None, as_json=False):
    """(status, headers, body) without following redirects."""
    body = None
    if data is not None:
        body = json.dumps(data).encode() if as_json else urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(base + path, data=body, method=method, headers=headers or {})
    if body is not None:
        req.add_header("Content-Type", "application/json" if as_json else "application/x-www-form-urlencoded")
    try:
        with OPENER.open(req) as r:
            return r.status, r.headers, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode()


@pytest.fixture
def users_file(tmp_path):
    path = tmp_path / "users"
    path.write_text(gate.format_users({"alice": gate.hash_password("wonderland", 1000), "bob": gate.hash_password("builder", 1000)}))
    return path


@pytest.fixture
def start(users_file, tmp_path):
    servers = []

    def make(**env):
        env = {"FAKECO_GATE_USERS_FILE": str(users_file), "FAKECO_GATE_SECRET_FILE": str(tmp_path / "secret"), "FAKECO_GATE_PORT": "0", **env}
        g = gate.Gate(env)
        server = gate.ThreadingHTTPServer(("127.0.0.1", 0), gate.make_handler(g))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return g, f"http://127.0.0.1:{server.server_address[1]}"

    yield make
    for s in servers:
        s.shutdown()


def sign_in(base, username="alice", password="wonderland"):
    status, _, body = call(base, "/gate/login", "POST", {"username": username, "password": password}, as_json=True)
    return status, (json.loads(body) if body else None)


def test_token_from_sign_in_passes_in_cookie_or_header(start):
    _, base = start()
    assert call(base, "/_verify")[0] == 401
    status, data = sign_in(base)
    assert status == 200 and data["username"] == "alice" and data["expires_at"] > time.time()
    status, headers, _ = call(base, "/_verify", headers={"X-Gate-Token": data["token"]})
    assert status == 204 and headers["X-Gate-User"] == "alice"
    assert call(base, "/_verify", headers={"Cookie": f"other=1; fakeco_gate={data['token']}"})[0] == 204
    changed = data["token"][:-1] + ("1" if data["token"][-1] == "0" else "0")   # always a different last character of the signature
    assert call(base, "/_verify", headers={"X-Gate-Token": changed})[0] == 401


def test_wrong_password_and_unknown_user_fail_the_same_way(start):
    _, base = start()
    wrong, unknown = sign_in(base, password="nope"), sign_in(base, username="mallory")
    assert wrong == unknown and wrong[0] == 401 and wrong[1]["error"]["code"] == "gate_login_failed"


def test_browser_form_sets_cookie_and_goes_to_next_page_on_this_site_only(start):
    _, base = start()
    status, headers, _ = call(base, "/gate/login", "POST", {"username": "Alice", "password": "wonderland", "next": "/ops/stories"})
    assert status == 303 and headers["Location"] == "/ops/stories"
    cookie = headers["Set-Cookie"]
    assert cookie.startswith("fakeco_gate=") and "HttpOnly" in cookie and "Secure" not in cookie
    for evil in ("https://evil.example/", "//evil.example/", "/\\evil.example", "/gate/login"):
        assert call(base, "/gate/login", "POST", {"username": "alice", "password": "wonderland", "next": evil})[1]["Location"] == "/"
    status, headers, _ = call(base, "/gate/login", "POST", {"username": "alice", "password": "wonderland"}, headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in headers["Set-Cookie"]
    status, _, page = call(base, "/gate/login", "POST", {"username": "alice", "password": "bad", "next": '"><script>'})
    assert status == 401 and "Wrong username or password" in page and "<script>" not in page


def test_changing_a_password_or_removing_a_user_signs_them_out(start, users_file):
    _, base = start()
    alice, bob = sign_in(base)[1]["token"], sign_in(base, "bob", "builder")[1]["token"]
    users = gate.parse_users(users_file.read_text())
    users["alice"] = gate.hash_password("new-password", 1000)
    del users["bob"]
    users_file.write_text(gate.format_users(users) + "# padding so that the size changes\n")
    assert call(base, "/_verify", headers={"X-Gate-Token": alice})[0] == 401
    assert call(base, "/_verify", headers={"X-Gate-Token": bob})[0] == 401
    assert sign_in(base, password="wonderland")[0] == 401
    assert sign_in(base, password="new-password")[0] == 200


def test_session_expires_after_the_configured_hours(start, monkeypatch):
    skew = [0]
    monkeypatch.setattr(gate, "time", types.SimpleNamespace(time=lambda: time.time() + skew[0], monotonic=time.monotonic, sleep=time.sleep))
    _, base = start(FAKECO_GATE_SESSION_HOURS="2")
    token = sign_in(base)[1]["token"]
    skew[0] = 2 * 3600 - 60
    assert call(base, "/_verify", headers={"X-Gate-Token": token})[0] == 204
    skew[0] = 2 * 3600 + 60
    assert call(base, "/_verify", headers={"X-Gate-Token": token})[0] == 401


def test_off_lets_everything_pass_but_no_users_lets_nothing_pass(start, users_file):
    _, base = start(FAKECO_GATE_ENABLED="off")
    assert call(base, "/_verify")[0] == 204
    users_file.write_text("")
    _, base = start()
    assert call(base, "/_verify")[0] == 401 and sign_in(base)[0] == 401


def test_ten_wrong_passwords_lock_the_client_out_for_a_minute(start, monkeypatch):
    monkeypatch.setattr(gate.time, "sleep", lambda s: None)
    _, base = start()
    for _ in range(gate.MAX_FAILURES):
        assert sign_in(base, password="nope")[0] == 401
    assert sign_in(base)[0] == 429     # even the right password
    status, _, _ = call(base, "/gate/login", "POST", {"username": "alice", "password": "wonderland"}, headers={"X-Forwarded-For": "10.1.1.1"})
    assert status == 303                # another client is not locked out


def test_users_command(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "users")
    monkeypatch.setenv("GATE_PASSWORD", "kept-secret")
    gate.users_command(path, ["add", "Carol"])
    assert "kept-secret" not in capsys.readouterr().out
    stored = gate.parse_users(open(path).read())
    assert list(stored) == ["carol"] and gate.check_password("kept-secret", stored["carol"]) and not gate.check_password("other", stored["carol"])
    monkeypatch.delenv("GATE_PASSWORD")
    gate.users_command(path, ["passwd", "carol"])
    assert "password: " in capsys.readouterr().out and not gate.check_password("kept-secret", gate.parse_users(open(path).read())["carol"])
    for bad in (["add", "carol"], ["passwd", "dave"], ["remove", "dave"], ["add", "a:b"], ["add", ".x"]):
        with pytest.raises(SystemExit):
            gate.users_command(path, bad)
    gate.users_command(path, ["remove", "carol"])
    assert gate.parse_users(open(path).read()) == {}
