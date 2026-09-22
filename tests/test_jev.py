from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import urllib.error

from agent_video.db import Store
from agent_video.jev import (
    DEFAULT_JEV_BASE_URL,
    DEFAULT_MIN_CONFIDENCE,
    JevAuthError,
    JevClient,
    JevError,
    JevRateLimitError,
    JevTimeoutError,
    build_candidate_audit_questions,
)
from agent_video.runner import JobRunner
from agent_video.server import Application


class JevClientTestCase(unittest.TestCase):
    def test_questions_builder(self):
        questions = build_candidate_audit_questions(main_product="白色真丝衬衫")
        self.assertIn("is_usable", questions)
        self.assertIn("standalone", questions)
        self.assertIn("content_type", questions)
        self.assertIn("selling_value", questions)
        self.assertIn("opening_suitability", questions)
        self.assertEqual(questions["is_usable"]["type"], "choice")
        self.assertIn("白色真丝衬衫", questions["is_usable"]["instructions"])

    def test_system_one_success(self):
        client = JevClient(api_key="test-key-123")
        mock_response_data = {
            "answers": {
                "is_usable": {"choice": "yes", "confidence": 0.95},
                "standalone": {"noul": 0.9},
                "content_type": {"choice": "material"},
                "selling_value": {"score": 3},
                "opening_suitability": {"score": 2},
            }
        }
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_cm = MagicMock()
            mock_cm.read.return_value = json.dumps(mock_response_data).encode("utf-8")
            mock_cm.__enter__.return_value = mock_cm
            mock_cm.__exit__.return_value = False
            mock_urlopen.return_value = mock_cm

            res = client.system_one(
                state="这件衣服面料是定制的丝光棉，不易变形",
                questions=build_candidate_audit_questions("T恤"),
            )
            self.assertEqual(res, mock_response_data)

    def test_system_one_auth_error(self):
        client = JevClient(api_key="bad-key")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                url="https://api.typesafe.ai/v1/systemone",
                code=401,
                msg="Unauthorized",
                hdrs={},
                fp=None,
            )
            with self.assertRaises(JevAuthError):
                client.system_one("test", {})

    def test_system_one_rate_limit_error(self):
        client = JevClient(api_key="key")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                url="https://api.typesafe.ai/v1/systemone",
                code=429,
                msg="Too Many Requests",
                hdrs={},
                fp=None,
            )
            with self.assertRaises(JevRateLimitError):
                client.system_one("test", {})

    def test_evaluate_candidate_confidence_gating(self):
        client = JevClient(api_key="test-key")

        # 1. 干净高置信度有效句子 -> keep
        with patch.object(
            client,
            "system_one",
            return_value={
                "answers": {
                    "is_usable": {"choice": "yes", "confidence": 0.92},
                    "standalone": {"noul": 0.8},
                    "content_type": {"choice": "material"},
                    "selling_value": {"score": 3},
                    "opening_suitability": {"score": 3},
                }
            },
        ):
            evaluation = client.evaluate_candidate(
                "领口是定制罗口，久穿不易变形",
                main_product="女装T恤",
                min_confidence=0.6,
            )
            self.assertEqual(evaluation.verdict, "keep")
            self.assertTrue(evaluation.is_usable)
            self.assertGreaterEqual(evaluation.confidence, 0.6)

        # 2. 错词低置信度句子 -> reject (置信度不足门控)
        with patch.object(
            client,
            "system_one",
            return_value={
                "answers": {
                    "is_usable": {"choice": "yes", "confidence": 0.35},
                    "standalone": {"noul": 0.4},
                    "content_type": {"choice": "garbled"},
                    "selling_value": {"score": 1},
                    "opening_suitability": {"score": 0},
                }
            },
        ):
            evaluation = client.evaluate_candidate(
                "一件提取，穿在身上特别舒服",
                main_product="女装T恤",
                min_confidence=0.6,
            )
            self.assertEqual(evaluation.verdict, "reject")
            self.assertIn("置信度不足", evaluation.reason)


class JevServerSettingsTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "data").mkdir(parents=True)
        self.app = Application(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_jev_settings_defaults_and_masking(self):
        settings = self.app.settings()
        self.assertIn("jev_enabled", settings)
        self.assertFalse(settings["jev_enabled"])
        self.assertIn("jev_base_url", settings)
        self.assertIn("jev_default_model", settings)
        self.assertIn("jev_min_confidence", settings)
        self.assertEqual(settings["jev_min_confidence"], 0.6)
        self.assertIn("jev_api_key_configured", settings)

    def test_jev_settings_update_and_mask_preservation(self):
        # 1. 首次配置明文 key
        updated = self.app.update_settings({
            "jev_enabled": True,
            "jev_api_key": "ts_live_secret_key_12345678",
            "jev_min_confidence": 0.75,
            "jev_concurrency": 12,
        })
        self.assertTrue(updated["jev_enabled"])
        self.assertTrue(updated["jev_api_key_configured"])
        self.assertTrue(updated["jev_api_key"].startswith("ts_****"))
        self.assertEqual(updated["jev_min_confidence"], 0.75)
        self.assertEqual(updated["jev_concurrency"], 12)

        # 数据库中实际保存的是完整真实 key
        stored_key = self.app.store.get_setting("jev_api_key")
        self.assertEqual(stored_key, "ts_live_secret_key_12345678")

        # 2. 前端再次回传带掩码的 key，不应抹掉真实 key
        updated2 = self.app.update_settings({
            "jev_api_key": updated["jev_api_key"],
            "jev_min_confidence": 0.8,
        })
        self.assertEqual(self.app.store.get_setting("jev_api_key"), "ts_live_secret_key_12345678")
        self.assertEqual(updated2["jev_min_confidence"], 0.8)


class JevRunnerAuditTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = Store(self.root / "test.db")
        self.runner = JobRunner(self.store, self.root)

    def tearDown(self):
        self.runner.stop()
        self.temp_dir.cleanup()

    def test_run_jev_audit_fallback_on_low_confidence(self):
        self.store.set_setting("jev_enabled", True)
        self.store.set_setting("jev_api_key", "mock-key")
        self.store.set_setting("jev_min_confidence", 0.6)

        job_id = self.store.create_job(
            title="测试任务",
            source_path="/tmp/fake.mp4",
            brief="",
            mode="fast",
            workspace=str(self.root / "workspace"),
        )
        job = {
            "id": job_id,
            "products": ["白色纯棉T恤"],
            "title": "测试任务",
        }

        engine_work = self.root / "workspace" / "engine"
        engine_work.mkdir(parents=True, exist_ok=True)

        candidates = [
            {"i": 1, "s": 0.0, "e": 3.0, "t": "领口久穿不松垮变形", "c": "material"},
            {"i": 2, "s": 3.0, "e": 6.0, "t": "一件提取真的太好穿了", "c": "selling"},
        ]

        # 模拟 evaluate_candidate: 第一条高置信度，第二条低置信度
        from agent_video.jev import JevEvaluation

        def mock_eval(text, **kwargs):
            if "一件提取" in text:
                return JevEvaluation(
                    verdict="reject",
                    is_usable=False,
                    confidence=0.32,
                    standalone=False,
                    content_type="garbled",
                    selling_value=10,
                    opening_suitability=0,
                    information_gain=10,
                    reason="错词低置信度",
                )
            return JevEvaluation(
                verdict="keep",
                is_usable=True,
                confidence=0.95,
                standalone=True,
                content_type="material",
                selling_value=85,
                opening_suitability=75,
                information_gain=80,
                reason="优质卖点",
            )

        with patch("agent_video.jev.JevClient.evaluate_candidate", side_effect=mock_eval):
            confident_decisions, unresolved, seconds = self.runner._run_jev_audit(
                job, engine_work, candidates
            )
            # 第一条被直接采纳为 confident decision
            self.assertEqual(len(confident_decisions), 1)
            self.assertEqual(confident_decisions[0]["candidate_id"], 1)
            self.assertEqual(confident_decisions[0]["verdict"], "keep")
            self.assertEqual(confident_decisions[0]["source"], "jev")

            # 第二条由于置信度 0.32 < 0.60，自动放入 unresolved 交由下游 LLM 兜底
            self.assertEqual(len(unresolved), 1)
            self.assertEqual(unresolved[0]["i"], 2)


if __name__ == "__main__":
    unittest.main()
