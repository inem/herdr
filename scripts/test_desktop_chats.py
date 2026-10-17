import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "src/platform/macos/desktop/bridge.py"
spec = importlib.util.spec_from_file_location("desktop_bridge", SOURCE)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
CID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class DesktopChats(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def claude(self):
        cwd = self.home / "project"
        cwd.mkdir()
        meta = self.home / "Library/Application Support/Claude/claude-code-sessions/account/org/local_chat.json"
        meta.parent.mkdir(parents=True)
        meta.write_text(json.dumps(dict(sessionId="local_chat", cliSessionId=CID, title="Desktop chat", cwd=str(cwd), lastActivityAt=1)))
        transcript = self.home / ".claude/projects/project" / (CID + ".jsonl")
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{"type":"user"}\n')
        return cwd, meta, transcript

    def test_claude_uses_cli_id_not_desktop_id_and_omits_missing_history(self):
        _, _, transcript = self.claude()
        rows = bridge.claude_chats(self.home)
        self.assertEqual(rows[0]["id"], CID)
        transcript.unlink()
        self.assertEqual(bridge.claude_chats(self.home), [])

    def test_archived_claude_chats_are_excluded(self):
        _, meta, _ = self.claude()
        data = json.loads(meta.read_text())
        data["isArchived"] = True
        meta.write_text(json.dumps(data))
        self.assertEqual(bridge.claude_chats(self.home), [])

    def test_work_filters_exact_desktop_origin_and_reads_wal(self):
        root = self.home / ".codex"
        root.mkdir()
        db = sqlite3.connect(root / "state_5.sqlite")
        self.addCleanup(db.close)
        db.execute("pragma journal_mode=WAL")
        db.execute("create table threads(id,title,name,cwd,updated_at,originator,archived)")
        for cid, origin in ((CID, "codex_work_desktop"), (OTHER, "codex_cli_rs")):
            db.execute("insert into threads values(?,?,?,?,?,?,0)", (cid, "title", "My Work", str(self.home), 10, origin))
        db.commit()
        with patch.dict(os.environ, {"CODEX_HOME": str(root)}):
            rows = bridge.work_chats(self.home)
        self.assertEqual([r["id"] for r in rows], [CID])
        self.assertEqual(rows[0]["title"], "My Work")

    def test_catalog_partial_failure_keeps_other_provider(self):
        self.claude()
        with patch.object(bridge, "work_chats", side_effect=sqlite3.OperationalError("busy")):
            result = bridge.catalog(self.home)
        self.assertEqual(len(result["chats"]), 1)
        self.assertEqual(result["warnings"], ["work: busy"])

    def test_antigravity_catalog_excludes_cli_and_nested_conversations(self):
        root = self.home / ".gemini/antigravity"
        root.mkdir(parents=True)
        with sqlite3.connect(root / "conversation_summaries.db") as db:
            db.execute("create table conversation_summaries(conversation_id,title,last_modified_time,app_data_dir,nesting_depth,step_count,source)")
            for app, depth, source in [("antigravity", 0, ""), ("antigravity-cli", 0, ""),
                                       ("antigravity", 1, ""), ("antigravity", 0, "CORTEX_TRAJECTORY_SOURCE_CLI")]:
                db.execute("insert into conversation_summaries values(?,?,?,?,?,?,?)",
                           (CID, "Desktop", 1, app, depth, 2, source))
        self.assertEqual(len(bridge.antigravity_chats(self.home)), 1)

    def test_no_resume_for_missing_original(self):
        with patch.object(bridge.os, "execv") as launch:
            with self.assertRaises(bridge.DesktopError):
                bridge.resume(self.home, "claude", CID)
            launch.assert_not_called()

    def test_claude_resume_keeps_exact_id_without_fork(self):
        cwd, _, _ = self.claude()
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/another-profile"}), patch.object(bridge.shutil, "which", return_value="/bin/claude"), patch.object(bridge.os, "chdir") as chdir, patch.object(bridge.os, "execv") as launch:
            bridge.resume(self.home, "claude", CID)
            self.assertNotIn("CLAUDE_CONFIG_DIR", os.environ)
        launch.assert_called_once_with("/bin/claude", ["/bin/claude", "--resume", CID])
        chdir.assert_called_once_with(cwd)

    def test_busy_claude_refuses_before_launch(self):
        self.claude()
        registry = self.home / ".claude/sessions"
        registry.mkdir()
        (registry / f"{os.getpid()}.json").write_text(json.dumps({"sessionId": CID}))
        with patch.object(bridge.os, "execv") as launch:
            with self.assertRaisesRegex(bridge.DesktopError, "already running"):
                bridge.resume(self.home, "claude", CID)
            launch.assert_not_called()

    def test_work_launch_is_dedicated_and_same_id(self):
        chat = dict(provider="work", id=CID, title="Work", cwd=str(self.home))
        with patch.object(bridge, "catalog", return_value={"chats": [chat], "warnings": []}), patch.object(bridge, "codex_executable", return_value="/bin/codex"), patch.object(bridge.os, "chdir"), patch.object(bridge.os, "execv") as launch:
            bridge.resume(self.home, "work", CID)
        launch.assert_called_once_with("/bin/codex", ["/bin/codex", "--no-daemon", "resume", CID])

    def test_metadata_reporting_failure_does_not_break_chat(self):
        with patch.dict(os.environ, {"HERDR_DESKTOP_EXE": "/missing", "HERDR_PANE_ID": "w1:p1"}), patch.object(bridge.subprocess, "run", side_effect=OSError("gone")):
            bridge.report_state("idle", CID, "hello")

    def test_hub_send_keeps_original_and_existing_model(self):
        class FakeHub:
            def __init__(self): self.calls = []
            def summary(self, cid): return {"stepCount": 17, "status": "CASCADE_RUN_STATUS_IDLE"}
            def rpc(self, method, body):
                self.calls.append((method, body))
                if method == "GetCascadeTrajectory":
                    return {"trajectory": {"cascadeId": CID, "executorMetadatas": [{
                        "cascadeConfig": {"plannerConfig": {"planModel": "MODEL_EXISTING", "toolConfig": {"unsafe": True}}},
                        "agentConfigResolution": {"requestedAgentSpec": {"agentName": "original"}}}]}}
                return {}
        hub = FakeHub()
        self.assertEqual(bridge.hub_send(hub, CID, "hello"), 17)
        method, payload = hub.calls[-1]
        self.assertEqual(method, "SendUserCascadeMessage")
        self.assertEqual(payload["cascadeId"], CID)
        self.assertEqual(payload["items"], [{"text": "hello"}])
        self.assertEqual(payload["cascadeConfig"]["plannerConfig"], {"planModel": "MODEL_EXISTING"})
        self.assertNotIn("StartCascade", [m for m, _ in hub.calls])

    def test_busy_hub_never_sends(self):
        class Busy:
            def summary(self, cid): return {"status": "CASCADE_RUN_STATUS_RUNNING"}
        with patch.object(bridge, "hub_config", return_value={}):
            with self.assertRaisesRegex(bridge.DesktopError, "busy"):
                bridge.hub_send(Busy(), CID, "hello")

    def test_hub_loads_latest_paginated_executor_and_rejects_identity_change(self):
        class Paginated:
            def __init__(self): self.calls = []
            def rpc(self, method, body):
                self.calls.append((method, body))
                if method == "GetCascadeTrajectory":
                    return {"trajectory": {"cascadeId": CID, "executorMetadatas": []}, "numTotalExecutorMetadata": 300}
                return {"executorMetadatas": [{"cascadeConfig": {"plannerConfig": {"planModel": "M_RECENT"}},
                    "agentConfigResolution": {"requestedAgentSpec": {"agentName": "same"}}}]}
        hub = Paginated()
        self.assertEqual(bridge.hub_config(hub, CID)["cascadeConfig"]["plannerConfig"]["planModel"], "M_RECENT")
        self.assertEqual(hub.calls[-1][1]["executorMetadataOffset"], 290)
        with patch.object(hub, "rpc", return_value={"trajectory": {"cascadeId": OTHER}}):
            with self.assertRaisesRegex(bridge.DesktopError, "different chat"):
                bridge.hub_send(hub, CID, "hello")

    def test_missing_model_refuses_send(self):
        class Missing:
            def rpc(self, method, body):
                self.assert_method = method
                return {"trajectory": {"cascadeId": CID}}
        hub = Missing()
        with self.assertRaisesRegex(bridge.DesktopError, "No model"):
            bridge.hub_send(hub, CID, "hello")
        self.assertEqual(hub.assert_method, "GetCascadeTrajectory")

    def test_loopback_only_and_no_redirects(self):
        for url in ["https://example.org/", "http://localhost:8000/", "http://u:p@127.0.0.1:80/", "file:///tmp/a"]:
            with self.assertRaises(bridge.DesktopError): bridge.local_origin(url)
        self.assertEqual(bridge.local_origin("https://127.0.0.1:8000/c/abc"), "https://127.0.0.1:8000")
        with self.assertRaises(bridge.DesktopError): bridge.NoRedirect().redirect_request(None)

    def test_terminal_content_cannot_inject_escape_sequences(self):
        self.assertNotIn("\x1b", bridge.clean("hi\x1b]52;c;payload\x07"))
        with self.assertRaises(bridge.DesktopError): bridge.identifier("--fork")


if __name__ == "__main__":
    unittest.main()
