"""Local desktop chat adapters. No import, fork, transcript writes or auth export.

Embedded in Herdr; only Python's standard library is required. Discovery is
read-only. The native CLIs retain their own trust and approval prompts.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

PROVIDERS = ("work", "claude", "antigravity")
SERVICE = "/exa.language_server_pb.LanguageServerService/"


class DesktopError(Exception):
    pass


def clean(text):
    # Titles, transcript text and errors are untrusted terminal content.
    return "".join(c for c in str(text) if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) < 160))


def identifier(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise DesktopError("Invalid chat ID") from error


def timestamp(value):
    if isinstance(value, (int, float)):
        return value / 1000 if value > 10**11 else value
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def read_db(path):
    # mode=ro includes the live WAL. immutable=1 would silently miss recent turns.
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute("pragma query_only=ON")
    return db


def row(provider, cid, title, cwd, updated, **extra):
    return dict(provider=provider, id=identifier(cid), title=clean(title or cid).replace("\n", " "),
                cwd=str(cwd or ""), updated=timestamp(updated), **extra)


def work_chats(home):
    root = Path(os.environ.get("CODEX_HOME", home / ".codex"))
    databases = sorted(root.glob("state_*.sqlite"), key=lambda p: int(re.search(r"_(\d+)", p.name)[1]), reverse=True)
    if not databases:
        return []
    with read_db(databases[0]) as db:
        columns = {r[1] for r in db.execute("pragma table_info(threads)")}
        if "originator" not in columns:
            raise DesktopError("This Codex history has no desktop origin metadata; update ChatGPT Work")
        # source='vscode' is shared by several clients; originator is the exact discriminator.
        name = "name" if "name" in columns else "NULL as name"
        rows = db.execute(f"select id,title,{name},cwd,updated_at,originator from threads where archived=0 and originator='codex_work_desktop'")
        return [row("work", r["id"], r["name"] or r["title"], r["cwd"], r["updated_at"]) for r in rows]


def claude_chats(home):
    root = home / "Library/Application Support/Claude/claude-code-sessions"
    result = []
    for path in root.glob("*/*/local_*.json"):
        try:
            data = read_json(path)
            if data.get("isArchived") or not data.get("cliSessionId"):
                continue
            cid = identifier(data["cliSessionId"])
            # Desktop metadata can outlive deleted transcripts. Never resume an empty replacement.
            transcripts = list((home / ".claude/projects").glob("*/" + cid + ".jsonl"))
            if not transcripts:
                continue
            result.append(row("claude", cid, data.get("title"), data.get("cwd"),
                              data.get("lastActivityAt"), desktop_id=data.get("sessionId"),
                              transcript=str(transcripts[0])))
        except (OSError, ValueError, DesktopError):
            continue
    return result


def antigravity_chats(home):
    path = home / ".gemini/antigravity/conversation_summaries.db"
    if not path.exists():
        return []
    with read_db(path) as db:
        # The CLI summary catalog includes desktop entries too. Only read the App's own store.
        rows = db.execute("select * from conversation_summaries where app_data_dir='antigravity' and nesting_depth=0 and step_count>0")
        return [row("antigravity", r["conversation_id"], r["title"], "", r["last_modified_time"])
                for r in rows if r["source"] != "CORTEX_TRAJECTORY_SOURCE_CLI"]


def catalog(home, provider=None):
    chats, warnings = [], []
    for name, discover in (("work", work_chats), ("claude", claude_chats), ("antigravity", antigravity_chats)):
        if provider and name != provider:
            continue
        try:
            chats.extend(discover(home))
        except (OSError, ValueError, sqlite3.Error, DesktopError) as error:
            warnings.append(f"{name}: {error}")
    unique = {(r["provider"], r["id"]): r for r in chats}
    return {"chats": sorted(unique.values(), key=lambda r: r["updated"], reverse=True), "warnings": warnings}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise DesktopError("The desktop server redirected a local request")


def local_origin(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.username or parsed.password:
        raise DesktopError("Desktop endpoint must be literal loopback")
    return f"{parsed.scheme}://127.0.0.1:{parsed.port}"


class Hub:
    def __init__(self, home):
        # The app uses a self-signed certificate. This opener is restricted to the
        # discovered literal loopback origin and never follows redirects or proxies.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
        port_file = home / "Library/Application Support/Antigravity/DevToolsActivePort"
        try:
            port = int(port_file.read_text().splitlines()[0])
            if not 0 < port < 65536:
                raise ValueError("port out of range")
            cdp = f"http://127.0.0.1:{port}"
            version = self.get(cdp + "/json/version")
            if "Antigravity/" not in version.get("User-Agent", ""):
                raise DesktopError("The discovered endpoint is not Antigravity App")
            origins = {local_origin(p["url"]) for p in self.get(cdp + "/json/list")
                       if p.get("type") == "page" and p.get("url", "").startswith(("http://127.0.0.1:", "https://127.0.0.1:"))}
            if len(origins) != 1:
                raise DesktopError("Antigravity App has no unambiguous local page")
            self.origin = origins.pop()
            page = self.get(self.origin + "/", text=True)
            match = re.search(r'"?csrfToken"?\s*:\s*"([^"\n]+)"', page)
            if not match:
                raise DesktopError("Antigravity App did not provide a local session token")
            self.token = match[1]
        except (OSError, ValueError, IndexError) as error:
            raise DesktopError("Open Antigravity App with its local debugging endpoint available, then retry") from error

    def get(self, url, text=False):
        local_origin(url)
        with self.opener.open(url, timeout=8) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise DesktopError("Desktop discovery response too large")
        return raw.decode() if text else json.loads(raw)

    def rpc(self, method, payload):
        request = urllib.request.Request(self.origin + SERVICE + method,
            data=json.dumps(payload).encode(), method="POST", headers={
                "Content-Type": "application/json", "Connect-Protocol-Version": "1",
                "x-codeium-csrf-token": self.token})
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise DesktopError("Desktop response too large")
            return json.loads(raw)
        except urllib.error.HTTPError as error:
            # Never expose headers/tokens or retry a send after an uncertain outcome.
            raise DesktopError(f"Desktop {method} failed (HTTP {error.code}); refresh before retrying") from error

    def summary(self, cid):
        summary = self.rpc("GetAllCascadeTrajectories", {"excludeSubtrajectories": True}).get("trajectorySummaries", {}).get(cid)
        if not summary:
            raise DesktopError("The original desktop chat is no longer available; no replacement was created")
        return summary

    def steps(self, cid, offset):
        return self.rpc("GetCascadeTrajectorySteps", {"cascadeId": cid, "stepOffset": offset,
            "trajectoryVerbosity": "CLIENT_TRAJECTORY_VERBOSITY_PROD_UI", "disableRehydration": False}).get("steps", [])


def text_of(step):
    if "userInput" in step:
        value = step["userInput"]
        return "You", value.get("userResponse") or value.get("text") or "".join(i.get("text", "") for i in value.get("items", []))
    if "plannerResponse" in step:
        value = step["plannerResponse"]
        return "Antigravity", value.get("response") or value.get("modifiedResponse") or ""
    if "errorMessage" in step:
        value = step["errorMessage"].get("error", {})
        return "Error", value.get("shortError") or value.get("userErrorMessage") or "Agent failed"
    return "", ""


def hub_config(hub, cid):
    state = hub.rpc("GetCascadeTrajectory", {"cascadeId": cid,
        "trajectoryVerbosity": "CLIENT_TRAJECTORY_VERBOSITY_PROD_UI", "disableRehydration": True})
    trajectory = state.get("trajectory", {})
    if trajectory.get("cascadeId") != cid:
        raise DesktopError("Desktop returned a different chat ID")
    total = state.get("numTotalExecutorMetadata", 0)
    executors = trajectory.get("executorMetadatas", [])
    if total > len(executors):
        executors = hub.rpc("GetCascadeTrajectoryExecutorMetadatas", {
            "cascadeId": cid, "executorMetadataOffset": max(0, total - 10)}).get("executorMetadatas", [])
    for metadata in reversed(executors):
        planner = metadata.get("cascadeConfig", {}).get("plannerConfig", {})
        model = planner.get("planModel")
        if model and model != "MODEL_UNSPECIFIED":
            spec = metadata.get("agentConfigResolution", {}).get("requestedAgentSpec")
            if not spec:
                raise DesktopError("No existing agent configuration; send a message in Desktop first")
            # Only select the existing model and agent. Never copy old permission
            # overrides or invent a default model or a new conversation.
            return {"cascadeConfig": {"plannerConfig": {"planModel": model},
                    "executorConfig": {"useCoreDirect": True}}, "customAgentSpec": spec}
    raise DesktopError("No model recorded for this chat; select one and send a message in Desktop first")


def hub_send(hub, cid, prompt):
    config = hub_config(hub, cid)
    summary = hub.summary(cid)
    if summary.get("status") != "CASCADE_RUN_STATUS_IDLE":
        raise DesktopError("This chat is busy in Desktop. Wait for it to finish, then send again")
    offset = int(summary.get("stepCount", 0))
    hub.rpc("SendUserCascadeMessage", {"cascadeId": cid, "items": [{"text": prompt}], **config})
    return offset


def report_state(state, cid, title):
    exe, pane = os.environ.get("HERDR_DESKTOP_EXE"), os.environ.get("HERDR_PANE_ID")
    if not exe or not pane:
        return
    # The adapter is a peer process using Herdr's public API, not private UI state.
    try:
        subprocess.run([exe, "pane", "report-agent", pane, "--source", "desktop:antigravity",
                        "--agent", "antigravity", "--state", state, "--agent-session-id", cid,
                        "--message", title], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.SubprocessError):
        pass  # A Herdr metadata failure must not interrupt the original chat.


def hub_chat(home, chat):
    hub = Hub(home)
    cid = chat["id"]
    summary = hub.summary(cid)
    print(f"Antigravity Desktop · {clean(chat['title'])}\nChat {cid}\n"
          "Same desktop chat. Approvals stay in Desktop. /refresh · /exit\n", flush=True)
    offset = max(0, int(summary.get("stepCount", 0)) - 12)
    seen = {}

    def refresh():
        nonlocal offset
        steps = hub.steps(cid, offset)
        for index, step in enumerate(steps, offset):
            role, body = text_of(step)
            body = clean(body)
            if body and seen.get(index) != body:
                previous = seen.get(index, "")
                print((body[len(previous):] if previous and body.startswith(previous) else f"\n{role}: {body}"), flush=True)
                seen[index] = body
        # Keep the last step in the next read so streaming updates aren't missed.
        if steps:
            offset += len(steps) - 1
            for index in list(seen):
                if index < offset:
                    del seen[index]

    try:
        refresh()
        while True:
            status = hub.summary(cid).get("status")
            report_state("idle" if status == "CASCADE_RUN_STATUS_IDLE" else "working", cid, chat["title"])
            try:
                prompt = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if prompt == "/exit":
                break
            if not prompt or prompt == "/refresh":
                refresh()
                continue
            try:
                offset = hub_send(hub, cid, prompt)
                seen.clear()
                report_state("working", cid, chat["title"])
                started = time.monotonic()
                while True:
                    refresh()
                    status = hub.summary(cid).get("status")
                    if status == "CASCADE_RUN_STATUS_IDLE":
                        refresh()
                        break
                    if time.monotonic() - started > 300:
                        print("Still running or waiting for approval. Check Desktop; /refresh reads the latest reply.")
                        break
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\nDetached from output; the desktop task continues. /refresh to read it.")
            except (DesktopError, OSError, ValueError) as error:
                print(clean(error))
    finally:
        exe, pane = os.environ.get("HERDR_DESKTOP_EXE"), os.environ.get("HERDR_PANE_ID")
        if exe and pane:
            try:
                subprocess.run([exe, "pane", "release-agent", pane, "--source", "desktop:antigravity", "--agent", "antigravity"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            except (OSError, subprocess.SubprocessError):
                pass


def codex_executable(home):
    override = os.environ.get("HERDR_DESKTOP_CODEX")
    candidates = ([override] if override else []) + [str(p) for p in Path("/Applications").glob("ChatGPT*.app/Contents/Resources/codex-cli/bin/codex")]
    candidates += [str(home / ".codex/packages/app-server-daemon/current/bin/codex"), shutil.which("codex")]
    for path in candidates:
        if path and Path(path).is_file() and os.access(path, os.X_OK):
            help_text = subprocess.run([path, "--help"], capture_output=True, text=True, timeout=10).stdout
            if "--no-daemon" in help_text:
                return path
    raise DesktopError("A current Codex CLI with --no-daemon is required. Open/update ChatGPT Work or set HERDR_DESKTOP_CODEX")


def claude_busy(home, cid):
    for path in (home / ".claude/sessions").glob("*.json"):
        try:
            record = read_json(path)
            if record.get("sessionId") != cid:
                continue
            pid = int(record.get("pid", path.stem))
            os.kill(pid, 0)
            return True
        except (OSError, ValueError, TypeError):
            continue
    return False


def resume(home, provider, cid):
    cid = identifier(cid)
    entries = catalog(home, provider)
    chat = next((r for r in entries["chats"] if r["id"] == cid), None)
    if not chat:
        raise DesktopError("Original desktop chat not found. " + "; ".join(entries["warnings"]))
    if provider == "antigravity":
        hub_chat(home, chat)
        return
    cwd = Path(chat["cwd"])
    if not chat["cwd"] or not cwd.is_dir():
        raise DesktopError("The original working folder is missing; restore it before resuming")
    if provider == "work":
        # A dedicated process releases its ownership on exit; the shared daemon
        # retains it after disconnect and can leave Desktop locked for 30 minutes.
        argv = [codex_executable(home), "--no-daemon", "resume", cid]
    else:
        if claude_busy(home, cid):
            raise DesktopError("This Claude session is already running. Close it there before resuming here")
        binary = shutil.which("claude")
        if not binary:
            raise DesktopError("Claude Code CLI is not installed")
        argv = [binary, "--resume", cid]
        # Desktop uses the default profile. Explicitly setting even ~/.claude
        # moves Claude's global config from ~/.claude.json into that directory.
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
    print(f"Continuing {clean(chat['title'])}\nSame chat ID: {cid}\n", flush=True)
    os.chdir(cwd)
    os.execv(argv[0], argv)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("list", "resume"))
    parser.add_argument("provider", nargs="?", choices=PROVIDERS)
    parser.add_argument("id", nargs="?")
    args = parser.parse_args()
    home = Path.home()
    try:
        if args.action == "list":
            print(json.dumps(catalog(home, args.provider), ensure_ascii=False))
        elif args.provider and args.id:
            resume(home, args.provider, args.id)
        else:
            parser.error("resume requires a provider and chat ID")
    except (DesktopError, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        print("Desktop chats: " + clean(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
