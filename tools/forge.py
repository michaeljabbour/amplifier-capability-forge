#!/usr/bin/env python3
"""forge.py — drive the forge terminal MCP daemon over HTTP JSON-RPC.

Forge (forge-terminal-mcp) hosts persistent PTY sessions at
http://127.0.0.1:3141/mcp (override with FORGE_URL). This client works even
when forge's MCP tools aren't connected to the calling harness — any agent
that can run python3 can spin up, fan out, drive, and tear down terminals.

Generic terminal commands:
  doctor                                   ensure daemon healthy (auto-fixes node-pty spawn-helper)
  new [--name N] [--cwd DIR] [--tag T]...  spawn PTY -> prints session id (sleep 2 before first cmd)
      [--cols 120] [--rows 36] [--program CMD] [--args ...] [--arg VALUE]...
  type <id> <text> [--no-newline]          send text
  key <id> <key>                           enter, escape, ctrl+c, up, down, tab... (fixed list)
  submit <id> <text>                       text + Escape+Enter (Claude Code prompt submission)
  run <id> <text> [--wait SECS]            type + enter + wait + print cleaned new output
  screen <id>                              rendered viewport (what a human sees NOW)
  read <id>                                incremental output since last read (ANSI-stripped)
  grep <id> <regex> [--max 50]             search scrollback (buffer has ANSI - grep single words)
  wait <id> <regex> [--timeout MS]         block until pattern or exit; exit code 1 on timeout
  exec <command> [--cwd DIR] [--timeout MS] [--shell SHELL]
                                              one-shot command, auto-cleanup, real exit code
  codex-exec <prompt> [--cwd DIR] [--sandbox MODE]
                                              one-shot Codex with explicit permissions
  list [--tag T]                           all sessions
  close <id> | close-tag <tag>             teardown one / a tagged fleet

Agent-CLI commands (forge natively wraps Claude Code, Codex, Gemini):
  spawn-claude  --cwd DIR [--prompt P] [--model M] [--one-shot] [--worktree --branch B]
                [--name N] [--tag T]... [--max-budget USD]
  spawn-codex   --cwd DIR [--prompt P] [--model M] [--one-shot] [--worktree --branch B]
  spawn-gemini  --cwd DIR [--prompt P] [--model M] [--one-shot] [--sandbox]
  delegate <agent> <prompt> [--cwd DIR] [--mode oneshot|interactive] [--session ID]
           [--model M] [--timeout MS] [--from LABEL] [--worktree --branch B]
  history <id> [--limit N]                 tool-call history of a claude/codex session

All spawn-* print the new session id; drive follow-ups with `submit` (claude)
or `type`+`key enter` (codex/gemini), and watch with `screen`/`wait`.
"""
import argparse
import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
from typing import Any

DEFAULT_URL = "http://127.0.0.1:3141/mcp"
URL = os.environ.get("FORGE_URL", DEFAULT_URL)
SESSION_FILE = os.path.expanduser("~/.cache/forge-py-mcp-session")

ANSI = re.compile(r"\x1b\][^\x07]*\x07|\x1b\[[0-9;?]*[a-zA-Z]|\x1b[=>]|\r")


def _endpoint():
    """The daemon is local unless the caller explicitly selects remote HTTPS."""
    try:
        if not isinstance(URL, str) or len(URL) > 2048 or any(not 33 <= ord(c) <= 126 for c in URL):
            raise ValueError
        parsed = urllib.parse.urlsplit(URL)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme not in {"http", "https"} or not host or
                parsed.username is not None or parsed.password is not None or
                parsed.query or parsed.fragment or "\\" in URL or "%" in host or
                (port is not None and not 1 <= port <= 65535)):
            raise ValueError
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host.lower() == "localhost"
        if not local and (parsed.scheme != "https" or os.environ.get("FORGE_ALLOW_REMOTE") != "1"):
            raise ValueError
        authority = "[" + host.lower() + "]" if ":" in host else host.lower()
        if port is not None:
            authority += ":" + str(port)
        return urllib.parse.urlunsplit((parsed.scheme, authority, parsed.path or "/", "", ""))
    except (ValueError, TypeError):
        raise SystemExit("forge: invalid endpoint; use loopback HTTP(S), or explicitly opt into remote HTTPS with FORGE_ALLOW_REMOTE=1. URL credentials, query strings and fragments are refused.") from None


def _session_id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 1024 or any(not 33 <= ord(c) <= 126 for c in value):
        raise SystemExit("forge: invalid daemon session identifier")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SystemExit("forge: endpoint redirects are refused; configure the reviewed final endpoint directly")


def _post(payload, sid=None):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if sid:
        headers["mcp-session-id"] = _session_id(sid)
    req = urllib.request.Request(_endpoint(), data=json.dumps(payload).encode(), headers=headers)
    # A configured environment proxy or HTTP redirect must not receive local
    # command/prompt data or the daemon's session header.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(req, timeout=630) as r:
        new_sid = r.headers.get("mcp-session-id")
        content_type = r.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        body = r.read().decode()
    if content_type == "application/json":
        value = json.loads(body)
        msgs = value if isinstance(value, list) else [value]
    else:
        msgs = []
        for block in body.split("\n\n"):
            data = "\n".join(l[6:] for l in block.split("\n") if l.startswith("data: "))
            if data:
                msgs.append(json.loads(data))
    if any(not isinstance(msg, dict) for msg in msgs):
        raise ValueError("Invalid protocol message shape")
    expected_id = payload.get("id")
    if expected_id is not None:
        matching = [msg for msg in msgs if msg.get("id") == expected_id]
        return (matching[-1] if matching else None), new_sid
    return (msgs[-1] if msgs else None), new_sid


def _request_id():
    return uuid.uuid4().hex


def _cache_parent():
    path = os.path.abspath(SESSION_FILE)
    parent, name = os.path.split(path)
    if not name or not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise SystemExit("forge: private session cache requires POSIX no-follow file operations")
    fd = None
    try:
        os.makedirs(parent, mode=0o700, exist_ok=True)
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise OSError
        return fd, name
    except OSError:
        if fd is not None:
            os.close(fd)
        raise SystemExit("forge: session cache directory must be owned, private from other writers, and not a symlink") from None


def _cached_session(endpoint):
    parent, name = _cache_parent()
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        except OSError:
            raise SystemExit("forge: refusing unsafe session cache file") from None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            # An atomic refresh may unlink this already opened private inode.
            # It remains safe to read; additional hard links are refused.
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink > 1:
                raise SystemExit("forge: refusing unsafe session cache file")
            if info.st_mode & 0o077 or info.st_size > 4096:
                return None
            raw = stream.read(4097)
        try:
            value = json.loads(raw)
            if (not isinstance(value, dict) or set(value) != {"version", "endpoint_sha256", "session_id"} or
                    type(value["version"]) is not int or value["version"] != 1 or
                    value["endpoint_sha256"] != hashlib.sha256(endpoint.encode()).hexdigest()):
                return None
            return _session_id(value["session_id"])
        except (ValueError, UnicodeError):
            return None  # Legacy unscoped text is never sent to a new endpoint.
    finally:
        os.close(parent)


def _save_session(endpoint, sid):
    content = json.dumps({"version": 1, "endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest(), "session_id": _session_id(sid)}).encode()
    parent, name = _cache_parent()
    temporary = ".forge-session-" + uuid.uuid4().hex
    try:
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise SystemExit("forge: refusing unsafe session cache file")
        except FileNotFoundError:
            pass
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def _handshake():
    endpoint = _endpoint()
    _, sid = _post({"jsonrpc": "2.0", "id": _request_id(), "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "forge-py", "version": "1.0"}}})
    sid = _session_id(sid)
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    _save_session(endpoint, sid)
    return sid


def _sid():
    return _cached_session(_endpoint()) or _handshake()


def call(tool: str, args: dict) -> Any:
    payload = {"jsonrpc": "2.0", "id": _request_id(), "method": "tools/call",
               "params": {"name": tool, "arguments": args}}
    try:
        try:
            msg, _ = _post(payload, _sid())
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise SystemExit(f"forge: request failed with HTTP {error.code}; outcome may be uncertain and was not retried") from None
            # Streamable HTTP defines 404 as an expired session. Other errors
            # do not establish that a mutating command was never performed.
            msg, _ = _post(payload, _handshake())
    except (urllib.error.URLError, http.client.HTTPException, OSError, UnicodeError, ValueError):
        raise SystemExit(
            "forge: transport or protocol failure; outcome may be uncertain and was not retried. Check the endpoint and daemon status; "
            "if Codex blocks localhost or user-cache access, request scoped permission."
        ) from None
    if not isinstance(msg, dict):
        raise SystemExit("forge: missing or invalid daemon response; inspect session state before retrying")
    if "error" in msg:
        code = msg["error"].get("code") if isinstance(msg["error"], dict) else None
        suffix = " " + str(code) if type(code) is int else ""
        raise SystemExit("forge: daemon returned a protocol error" + suffix + "; inspect session state locally before retrying")
    res = msg.get("result")
    if not isinstance(res, dict) or not isinstance(res.get("content", []), list):
        raise SystemExit("forge: invalid daemon tool response")
    if res.get("isError"):
        raise SystemExit("forge: daemon tool reported an error; inspect session state locally before retrying")
    content = res.get("content", [])
    if any(not isinstance(c, dict) or (c.get("type") == "text" and not isinstance(c.get("text", ""), str)) for c in content):
        raise SystemExit("forge: invalid daemon content")
    texts = [c.get("text", "") for c in content if c.get("type") == "text"]
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except (json.JSONDecodeError, ValueError):
        return joined


def clean(s):
    return ANSI.sub("", s or "")


def _print_data(out):
    if isinstance(out, dict):
        print(clean(out.get("data", "")))
    else:
        print(clean(str(out)))


def _require_executable(name, install_hint):
    executable = shutil.which(name)
    if executable:
        return executable
    raise SystemExit(f"forge: `{name}` executable not found. {install_hint}")


def _resolve_shell(explicit=None):
    if explicit:
        executable = explicit if os.path.sep in explicit else shutil.which(explicit)
        if executable and os.path.isfile(executable) and os.access(executable, os.X_OK):
            return executable
        raise SystemExit(f"forge: shell is not executable or was not found: {explicit}")

    candidates = [os.environ.get("SHELL"), shutil.which("bash"), shutil.which("sh"), "/bin/sh"]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise SystemExit("forge: no executable shell found; pass `exec --shell /path/to/shell`")


def _finish_command(out):
    if isinstance(out, dict):
        print(clean(out.get("output", "")))
        return out.get("exitCode", 0) or 0
    print(clean(str(out)))
    return 0


def doctor():
    forge = _require_executable(
        "forge", "Review and install a pinned forge-terminal-mcp release using its official documentation, then rerun. This helper does not download or install it."
    )
    status_result = subprocess.run([forge, "status"], capture_output=True, text=True)
    status = status_result.stdout + status_result.stderr
    if status_result.returncode or "not running" in status.lower() or "running" not in status.lower():
        started = subprocess.run([forge, "start", "-d"], capture_output=True, text=True)
        if started.returncode:
            detail = (started.stderr or started.stdout).strip()
            raise SystemExit(f"forge: daemon failed to start: {detail or 'unknown error'}")
        time.sleep(1)
    try:
        info = call("create_terminal", {"name": "forge-py-doctor"})
        call("close_terminal", {"id": info["id"]})
        print("forge: healthy")
        return
    except SystemExit as e:
        if "posix_spawnp" not in str(e):
            raise
    # node-pty's spawn-helper loses its exec bit after `bun install -g` updates
    import glob
    fixed = []
    for helper in glob.glob(os.path.expanduser(
            "~/.bun/install/global/node_modules/node-pty/prebuilds/*/spawn-helper")):
        os.chmod(helper, 0o755)
        fixed.append(helper)
    subprocess.run([forge, "stop"], capture_output=True, text=True)
    time.sleep(1)
    restarted = subprocess.run([forge, "start", "-d"], capture_output=True, text=True)
    if restarted.returncode:
        detail = (restarted.stderr or restarted.stdout).strip()
        raise SystemExit(f"forge: daemon failed to restart after repair: {detail or 'unknown error'}")
    time.sleep(1)
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    info = call("create_terminal", {"name": "forge-py-doctor"})
    call("close_terminal", {"id": info["id"]})
    print(f"forge: healthy (fixed exec bit on {len(fixed)} spawn-helper binaries, restarted daemon)")


def _spawn_args(a):
    args = {"cwd": os.path.abspath(a.cwd)}
    if a.prompt: args["prompt"] = a.prompt
    if a.model: args["model"] = a.model
    if a.name: args["name"] = a.name
    if a.tag: args["tags"] = a.tag
    if a.one_shot: args["oneShot"] = True
    if a.worktree:
        args["worktree"] = True
        if not a.branch:
            raise SystemExit("forge: --worktree requires --branch")
        args["branch"] = a.branch
    return args


def main():
    p = argparse.ArgumentParser(prog="forge.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")

    n = sub.add_parser("new")
    n.add_argument("--name"); n.add_argument("--cwd"); n.add_argument("--program")
    n.add_argument("--args", nargs="*"); n.add_argument("--tag", action="append")
    n.add_argument(
        "--arg", action="append",
        help="literal program argument; repeat and use --arg=VALUE for values beginning with '-'",
    )
    n.add_argument("--cols", type=int, default=120); n.add_argument("--rows", type=int, default=36)

    t = sub.add_parser("type"); t.add_argument("id"); t.add_argument("text")
    t.add_argument("--no-newline", action="store_true")
    k = sub.add_parser("key"); k.add_argument("id"); k.add_argument("key")
    sm = sub.add_parser("submit"); sm.add_argument("id"); sm.add_argument("text")
    r = sub.add_parser("run"); r.add_argument("id"); r.add_argument("text")
    r.add_argument("--wait", type=float, default=4)
    s = sub.add_parser("screen"); s.add_argument("id")
    rd = sub.add_parser("read"); rd.add_argument("id")
    g = sub.add_parser("grep"); g.add_argument("id"); g.add_argument("regex")
    g.add_argument("--max", type=int, default=50)
    w = sub.add_parser("wait"); w.add_argument("id"); w.add_argument("regex")
    w.add_argument("--timeout", type=int, default=60000)
    e = sub.add_parser("exec"); e.add_argument("command"); e.add_argument("--cwd")
    e.add_argument("--timeout", type=int, default=120000)
    e.add_argument("--shell", help="shell executable (default: $SHELL, then bash/sh)")
    cx = sub.add_parser("codex-exec")
    cx.add_argument("prompt"); cx.add_argument("--cwd")
    cx.add_argument("--sandbox", choices=["read-only", "workspace-write", "danger-full-access"],
                    default="read-only")
    cx.add_argument("--model"); cx.add_argument("--timeout", type=int, default=300000)
    cx.add_argument("--skip-git-repo-check", action="store_true")
    cx.add_argument("--ephemeral", action="store_true")
    cx.add_argument("--json", action="store_true")
    ls = sub.add_parser("list"); ls.add_argument("--tag")
    c = sub.add_parser("close"); c.add_argument("id")
    ct = sub.add_parser("close-tag"); ct.add_argument("tag")

    for agent in ("spawn-claude", "spawn-codex", "spawn-gemini"):
        sp = sub.add_parser(agent)
        sp.add_argument("--cwd", required=True); sp.add_argument("--prompt")
        sp.add_argument("--model"); sp.add_argument("--name")
        sp.add_argument("--tag", action="append")
        sp.add_argument("--one-shot", action="store_true")
        sp.add_argument("--worktree", action="store_true"); sp.add_argument("--branch")
        if agent == "spawn-claude":
            sp.add_argument("--max-budget", type=float)
        if agent == "spawn-gemini":
            sp.add_argument("--sandbox", action="store_true")

    d = sub.add_parser("delegate")
    d.add_argument("agent", choices=["claude", "codex", "gemini"])
    d.add_argument("prompt")
    d.add_argument("--cwd"); d.add_argument("--mode", choices=["oneshot", "interactive"])
    d.add_argument("--session"); d.add_argument("--model")
    d.add_argument("--timeout", type=int); d.add_argument("--from", dest="from_label")
    d.add_argument("--worktree", action="store_true"); d.add_argument("--branch")

    h = sub.add_parser("history"); h.add_argument("id"); h.add_argument("--limit", type=int)

    a = p.parse_args()

    if a.cmd == "doctor":
        doctor()
    elif a.cmd == "new":
        args = {"cols": a.cols, "rows": a.rows}
        if a.name: args["name"] = a.name
        if a.cwd: args["cwd"] = os.path.abspath(a.cwd)
        if a.tag: args["tags"] = a.tag
        if a.program: args["command"] = a.program
        program_args = (a.args or []) + (a.arg or [])
        if program_args: args["args"] = program_args
        print(call("create_terminal", args)["id"])
    elif a.cmd == "type":
        call("write_terminal", {"id": a.id, "input": a.text, "newline": not a.no_newline})
    elif a.cmd == "key":
        call("send_control", {"id": a.id, "key": a.key})
    elif a.cmd == "submit":
        # Send text and controls separately so Claude does not treat Escape+Enter
        # as part of a paste burst and leave the prompt sitting in the composer.
        call("write_terminal", {"id": a.id, "input": a.text, "newline": False})
        time.sleep(0.4)
        call("send_control", {"id": a.id, "key": "escape"})
        time.sleep(0.1)
        call("send_control", {"id": a.id, "key": "enter"})
    elif a.cmd == "run":
        call("read_terminal", {"id": a.id})  # drain
        call("write_terminal", {"id": a.id, "input": a.text, "newline": False})
        time.sleep(0.4)
        call("send_control", {"id": a.id, "key": "enter"})
        time.sleep(a.wait)
        _print_data(call("read_terminal", {"id": a.id}))
    elif a.cmd == "screen":
        print(call("read_screen", {"id": a.id}))
    elif a.cmd == "read":
        _print_data(call("read_terminal", {"id": a.id}))
    elif a.cmd == "grep":
        out = call("grep_terminal", {"id": a.id, "pattern": a.regex, "maxMatches": a.max})
        for m in out.get("matches", []):
            print(clean(m.get("text", "")))
    elif a.cmd == "wait":
        out = call("wait_for", {"id": a.id, "pattern": a.regex, "timeoutMs": a.timeout})
        print(json.dumps(out))
        if isinstance(out, dict) and not out.get("matched"):
            sys.exit(1)
    elif a.cmd == "exec":
        args = {"command": _resolve_shell(a.shell), "args": ["-lc", a.command],
                "timeoutMs": a.timeout, "cwd": os.path.abspath(a.cwd or os.getcwd())}
        sys.exit(_finish_command(call("run_command", args)))
    elif a.cmd == "codex-exec":
        codex = _require_executable("codex", "Install Codex CLI and authenticate it before delegating.")
        codex_args = ["exec", "--sandbox", a.sandbox]
        if a.model: codex_args.extend(["--model", a.model])
        if a.skip_git_repo_check: codex_args.append("--skip-git-repo-check")
        if a.ephemeral: codex_args.append("--ephemeral")
        if a.json: codex_args.append("--json")
        codex_args.append(a.prompt)
        args = {"command": codex, "args": codex_args, "timeoutMs": a.timeout,
                "cwd": os.path.abspath(a.cwd or os.getcwd())}
        sys.exit(_finish_command(call("run_command", args)))
    elif a.cmd == "list":
        args = {"tag": a.tag} if a.tag else {}
        print(json.dumps(call("list_terminals", args), indent=2))
    elif a.cmd == "close":
        call("close_terminal", {"id": a.id})
    elif a.cmd == "close-tag":
        call("close_group", {"tag": a.tag})
    elif a.cmd in ("spawn-claude", "spawn-codex", "spawn-gemini"):
        args = _spawn_args(a)
        if a.cmd == "spawn-claude" and a.max_budget:
            args["maxBudget"] = a.max_budget
        if a.cmd == "spawn-gemini" and a.sandbox:
            args["sandbox"] = True
        tool = a.cmd.replace("-", "_")
        out = call(tool, args)
        print(out["id"] if isinstance(out, dict) and "id" in out else json.dumps(out, indent=2))
    elif a.cmd == "delegate":
        args = {"prompt": a.prompt, "agent": a.agent}
        if a.session:
            args["sessionId"] = a.session  # follow-up: agent field is ignored by forge
        if a.cwd: args["cwd"] = os.path.abspath(a.cwd)
        if a.mode: args["mode"] = a.mode
        if a.model: args["model"] = a.model
        if a.timeout: args["timeout"] = a.timeout
        if a.from_label: args["from"] = a.from_label
        if a.worktree:
            args["worktree"] = True
            if not a.branch:
                raise SystemExit("forge: --worktree requires --branch")
            args["branch"] = a.branch
        out = call("delegate_task", args)
        print(out if isinstance(out, str) else json.dumps(out, indent=2))
    elif a.cmd == "history":
        args = {"id": a.id}
        if a.limit: args["limit"] = a.limit
        out = call("get_session_history", args)
        print(out if isinstance(out, str) else json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
