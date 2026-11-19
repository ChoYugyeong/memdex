"""Provider protocol failures must never become a successful review."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from memdex import review_providers as providers


class ProviderTests(unittest.TestCase):
    def test_deepseek_finds_installed_host_when_appdata_is_redirected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = root / "AppData/Local/Programs/DeepSeek Harness"
            (install / "resources").mkdir(parents=True)
            (install / "resources/app.asar").touch()
            host = install / "DeepSeek Harness.exe"
            host.touch()
            with patch.dict(os.environ, {"LOCALAPPDATA": str(root / "redirected")}), patch.object(Path, "home", return_value=root):
                command, env = providers.deepseek_command()
                self.assertEqual(command[0], str(host))
                self.assertEqual(env["ELECTRON_RUN_AS_NODE"], "1")

    def test_selection_rejects_unsupported_and_requires_local_model(self):
        self.assertEqual(providers.selection("qwen", ""), "qwen2.5")
        self.assertEqual(providers.selection("deepseek", ""), "deepseek-flash")
        for provider, model in (("unknown", ""), ("ollama", ""), ("claude", "different"), ("qwen", "has space")):
            with self.assertRaises(ValueError):
                providers.selection(provider, model)

    def test_ollama_preflight_rejects_missing_model_and_oversized_evidence(self):
        with patch.object(providers, "local_json", return_value={"models": [{"name": "qwen2.5:latest"}]}) as call:
            providers.check_ollama("qwen2.5", "short")
            with self.assertRaisesRegex(ValueError, "not installed"):
                providers.check_ollama("other", "short")
            count = call.call_count
            with self.assertRaisesRegex(ValueError, "12000"):
                providers.check_ollama("qwen2.5", "한" * 4001)
            self.assertEqual(call.call_count, count)

    def test_ollama_preserves_prompt_and_reports_token_limit(self):
        with patch.object(providers, "check_ollama"), patch.object(providers, "local_json", return_value={
                "done": True, "done_reason": "length", "response": "partial"}) as call:
            state, text = providers.run_ollama("complete payload", "qwen2.5", "system", 120)
        self.assertEqual(state, "truncated")
        self.assertIn("partial", text)
        self.assertEqual(call.call_args.args[1]["prompt"], "complete payload")
        self.assertEqual(call.call_args.args[1]["options"]["num_ctx"], 16384)

    def test_ollama_empty_unconfirmed_and_unreachable_are_failures(self):
        for event in ({"done": False, "response": "premature"}, {"done": True, "response": ""}, []):
            with patch.object(providers, "check_ollama"), patch.object(providers, "local_json", return_value=event):
                self.assertEqual(providers.run_ollama("payload", "qwen2.5", "sys", 120)[0], "failed")
        with patch.object(providers, "local_json", side_effect=OSError("private")):
            with self.assertRaisesRegex(ValueError, "unavailable"):
                providers.check_ollama("qwen2.5", "payload")

    def test_deepseek_requires_completed_final_and_no_tool_call(self):
        def parse(events, code=0):
            return providers.parse_deepseek(subprocess.CompletedProcess([], code,
                "diagnostic\n" + "\n".join(json.dumps(e) for e in events), ""))
        final = {"type": "final", "reason": {"kind": "completed"}, "text": "review"}
        self.assertEqual(parse([final]), ("completed", "review"))
        self.assertEqual(parse([final], code=1)[0], "failed")
        self.assertEqual(parse([{"type": "tool_call"}, final])[0], "failed")
        self.assertEqual(parse([{"type": "final", "reason": None, "text": "guess"}])[0], "failed")
        error = {"type": "final", "reason": {"kind": "error", "error": {"code": "MISSING_CREDENTIAL"}}, "text": ""}
        self.assertIn("MISSING_CREDENTIAL", parse([error], code=1)[1])

    def test_deepseek_overlay_retains_executor_but_disables_tools_and_connectors(self):
        names = ["tools", "agent-loop", "headless-runner", "credentials", "authorization", "deepseek-account",
                 "agent-default-model", "session-persistence-jsonl", "session-query-sqlite", "tool-fs",
                 "tool-executor-native", "agent-instructions", "custom-connector"]
        config = "\n".join(f"- id: {name}\n  name: '@deepseek-ai/" +
                           ("dsh-mcp-client" if name == "custom-connector" else name) + "'" for name in names)
        overlay = {row["id"]: row for row in providers.deepseek_overlay(config, "deepseek-flash")}
        for name in ("tool-fs", "custom-connector", "agent-instructions", "session-persistence-jsonl", "session-query-sqlite"):
            self.assertTrue(overlay[name]["disabled"])
        self.assertNotIn("tools", overlay)
        self.assertNotIn("tool-executor-native", overlay)
        self.assertFalse(overlay["credentials"]["disabled"])
        self.assertEqual(overlay["agent-default-model"]["config"]["provider"], "deepseek-account")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            providers.deepseek_overlay("unexpected format", "deepseek-flash")


if __name__ == "__main__":
    unittest.main()
