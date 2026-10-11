"""The gate that keeps the credentialed tools off an open endpoint.

Everything here is exercised over real HTTP against a running server, because
the failure this guards against is not a wrong return value - it is a tool that
turns out to be reachable. A unit test of the predicate would pass just as
happily with the middleware unregistered.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from meny_mcp import auth  # noqa: E402

TOKEN = "test-token-that-is-long-enough-x"
GATED = "mealie_inventory"
OPEN = "search_products"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Client:
    """Just enough MCP-over-HTTP to list and call tools."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.session: str | None = None

    def rpc(self, method: str, params: dict | None = None,
            headers: dict | None = None) -> dict:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params or {}}).encode()
        head = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}
        if self.session:
            head["mcp-session-id"] = self.session
        head.update(headers or {})
        req = urllib.request.Request(self.base, data=body, headers=head)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.session = resp.headers.get("mcp-session-id") or self.session
                raw = resp.read().decode()
        except urllib.error.HTTPError as exc:
            return {"http_status": exc.code, "body": exc.read().decode()[:200]}
        for line in raw.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        return json.loads(raw) if raw.strip() else {}

    def handshake(self, headers: dict | None = None) -> None:
        self.rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}}, headers)
        body = json.dumps({"jsonrpc": "2.0",
                           "method": "notifications/initialized"}).encode()
        head = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"}
        if self.session:
            head["mcp-session-id"] = self.session
        head.update(headers or {})
        try:
            urllib.request.urlopen(
                urllib.request.Request(self.base, data=body, headers=head),
                timeout=10)
        except urllib.error.HTTPError:
            pass

    def tool_names(self, headers: dict | None = None) -> list[str]:
        out = self.rpc("tools/list", {}, headers)
        return [t["name"] for t in out.get("result", {}).get("tools", [])]


class Gate(unittest.TestCase):
    proc: subprocess.Popen | None = None

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        env = dict(
            os.environ,
            PYTHONPATH=str(ROOT / "src"),
            MENY_TRANSPORT="http",
            MENY_HTTP_HOST="127.0.0.1",
            MENY_HTTP_PORT=str(cls.port),
            MENY_HTTP_PATH="/mcp",
            MENY_HTTP_JSON_RESPONSE="1",
            MENY_AUTH_TOKEN=TOKEN,
            # Enough to register the toolset; no call in this file reaches it.
            MEALIE_URL="http://127.0.0.1:1",
            MEALIE_TOKEN="unused",
        )
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "meny_mcp.server"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 30
        while time.time() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", cls.port)) == 0:
                    return
            if cls.proc.poll() is not None:
                raise RuntimeError("server exited during startup")
            time.sleep(0.2)
        raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        if cls.proc:
            cls.proc.terminate()
            cls.proc.wait(timeout=10)

    def url(self, suffix: str = "") -> str:
        return f"http://127.0.0.1:{self.port}/mcp{suffix}"

    # -- the open half stays open -----------------------------------------
    def test_the_grocery_tools_need_no_secret(self):
        c = Client(self.url())
        c.handshake()
        self.assertIn(OPEN, c.tool_names())

    # -- the gated half is hidden -----------------------------------------
    def test_an_anonymous_caller_cannot_see_the_mealie_tools(self):
        c = Client(self.url())
        c.handshake()
        names = c.tool_names()
        for gated in ("mealie_inventory", "save_week_manifest",
                      "apply_week_manifest", "verify_week_run",
                      "week_menu_for_planning"):
            self.assertNotIn(gated, names)

    def test_an_anonymous_caller_cannot_call_one_it_guessed(self):
        c = Client(self.url())
        c.handshake()
        out = c.rpc("tools/call", {"name": GATED, "arguments": {}})
        self.assertIn("error", out, out)
        self.assertIn("requires authentication", out["error"]["message"])

    # -- each accepted carrier --------------------------------------------
    def test_the_secret_path_unlocks_them(self):
        c = Client(self.url(f"/{TOKEN}"))
        c.handshake()
        self.assertIn(GATED, c.tool_names())

    def test_a_bearer_header_unlocks_them(self):
        head = {"Authorization": f"Bearer {TOKEN}"}
        c = Client(self.url())
        c.handshake(head)
        self.assertIn(GATED, c.tool_names(head))

    def test_a_query_parameter_unlocks_them(self):
        c = Client(self.url(f"?token={TOKEN}"))
        c.handshake()
        self.assertIn(GATED, c.tool_names())

    # -- and each way of getting it wrong ---------------------------------
    def test_a_wrong_secret_in_the_path_is_not_a_way_in(self):
        c = Client(self.url("/not-the-token-but-long-enough-x"))
        c.handshake()
        self.assertEqual(c.tool_names(), [])   # 404s: no session, no tools

    def test_a_wrong_bearer_token_is_refused(self):
        head = {"Authorization": "Bearer wrong-token-of-the-same-length-x"}
        c = Client(self.url())
        c.handshake(head)
        self.assertNotIn(GATED, c.tool_names(head))

    def test_a_prefix_of_the_token_is_refused(self):
        head = {"Authorization": f"Bearer {TOKEN[:-1]}"}
        c = Client(self.url())
        c.handshake(head)
        self.assertNotIn(GATED, c.tool_names(head))

    def test_authorising_the_handshake_does_not_authorise_later_calls(self):
        """The secret is checked per request, not remembered on the session."""
        head = {"Authorization": f"Bearer {TOKEN}"}
        c = Client(self.url())
        c.handshake(head)
        self.assertNotIn(GATED, c.tool_names())        # no header this time
        self.assertIn(GATED, c.tool_names(head))


class TokenRules(unittest.TestCase):
    def test_a_short_token_is_refused_rather_than_trusted(self):
        self.assertIsNotNone(auth.token_problem("short"))
        self.assertIsNotNone(auth.token_problem(""))
        self.assertIsNone(auth.token_problem("x" * auth.MIN_TOKEN_LENGTH))

    def test_stdio_needs_no_token(self):
        self.assertTrue(auth.authorised(None, ""))

    def test_an_http_request_without_a_configured_token_never_passes(self):
        class Req:
            headers = {"authorization": "Bearer anything"}
            scope: dict = {}
            query_params: dict = {}
        self.assertFalse(auth.authorised(Req(), ""))

    def test_startup_says_which_state_it_is_in(self):
        self.assertIn("off", auth.describe("", "/mcp", False)[0])
        self.assertIn("HIDDEN", auth.describe("", "/mcp", True)[0])
        self.assertIn("gated", auth.describe("x" * 40, "/mcp", True)[0])


if __name__ == "__main__":
    unittest.main()


class NoTokenConfigured(unittest.TestCase):
    """Mealie configured, MENY_AUTH_TOKEN absent.

    This is the dangerous middle state - a credentialed toolset on an open
    endpoint - and it is the one an operator reaches by setting MEALIE_URL and
    reading no further. It must fail closed.
    """

    proc: subprocess.Popen | None = None

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"),
                   MENY_TRANSPORT="http", MENY_HTTP_HOST="127.0.0.1",
                   MENY_HTTP_PORT=str(cls.port), MENY_HTTP_PATH="/mcp",
                   MENY_HTTP_JSON_RESPONSE="1",
                   MEALIE_URL="http://127.0.0.1:1", MEALIE_TOKEN="unused")
        env.pop("MENY_AUTH_TOKEN", None)
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "meny_mcp.server"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 30
        while time.time() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", cls.port)) == 0:
                    return
            if cls.proc.poll() is not None:
                raise RuntimeError("server exited during startup")
            time.sleep(0.2)
        raise RuntimeError("server did not start")

    @classmethod
    def tearDownClass(cls):
        if cls.proc:
            cls.proc.terminate()
            cls.proc.wait(timeout=10)

    def test_the_mealie_tools_are_not_exposed_at_all(self):
        c = Client(f"http://127.0.0.1:{self.port}/mcp")
        c.handshake()
        names = c.tool_names()
        self.assertIn(OPEN, names)
        self.assertNotIn(GATED, names)

    def test_and_cannot_be_called_by_name(self):
        c = Client(f"http://127.0.0.1:{self.port}/mcp")
        c.handshake()
        out = c.rpc("tools/call", {"name": GATED, "arguments": {}})
        self.assertIn("error", out, out)

    def test_a_guessed_bearer_token_does_not_help(self):
        head = {"Authorization": "Bearer anything-at-all-of-decent-length"}
        c = Client(f"http://127.0.0.1:{self.port}/mcp")
        c.handshake(head)
        self.assertNotIn(GATED, c.tool_names(head))


class StdioIsExempt(unittest.TestCase):
    def test_a_local_process_sees_the_tools_without_a_token(self):
        """No request to authenticate, and no network to reach it over."""
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"),
                   MENY_TRANSPORT="stdio",
                   MEALIE_URL="http://127.0.0.1:1", MEALIE_TOKEN="unused")
        env.pop("MENY_AUTH_TOKEN", None)
        proc = subprocess.Popen(
            [sys.executable, "-m", "meny_mcp.server"], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        # Kill on a watchdog rather than closing stdin early: an EOF that
        # arrives before the server has answered looks exactly like a server
        # that refused to answer.
        watchdog = threading.Timer(45, proc.kill)
        watchdog.start()

        def send(msg: dict) -> None:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()

        def await_id(want: int) -> dict:
            while True:
                line = proc.stdout.readline()
                if not line:
                    self.fail(f"server closed before answering id={want}")
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == want:
                    return msg

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18",
                             "capabilities": {},
                             "clientInfo": {"name": "t", "version": "0"}}})
            await_id(1)
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                  "params": {}})
            names = [t["name"] for t in await_id(2)["result"]["tools"]]
        finally:
            watchdog.cancel()
            proc.kill()
            proc.wait(timeout=10)

        self.assertIn(GATED, names)
        self.assertIn(OPEN, names)
