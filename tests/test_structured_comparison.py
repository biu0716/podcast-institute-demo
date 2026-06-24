import tempfile
import unittest
from pathlib import Path

import server


class StructuredComparisonTest(unittest.TestCase):
    def test_extract_json_object_from_fenced_output(self):
        data = server.extract_json_object('说明\n```json\n{"claims": [], "subtopics": []}\n```\n')

        self.assertEqual(data, {"claims": [], "subtopics": []})

    def test_extract_json_object_from_leading_text(self):
        data = server.extract_json_object('noise {"claims": [{"claim_id": "C01"}], "subtopics": []}')

        self.assertEqual(data["claims"][0]["claim_id"], "C01")

    def test_render_structured_comparison_files(self):
        selected = [
            {
                "episode_id": "ep1",
                "title": "Agent 投资与商业化",
                "podcast": "硅谷101",
                "angle": "Agent 投资",
                "description": "讨论 Agent 从 Demo 走向商业化。",
                "source_class": "source-intro",
            },
            {
                "episode_id": "ep2",
                "title": "AI 原生软件范式",
                "podcast": "科技慢半拍",
                "angle": "AI 原生软件",
                "description": "讨论 AI 原生应用范式变化。",
                "source_class": "source-audio",
            },
        ]
        comparison = {
            "claims": [
                {
                    "claim_id": "C01",
                    "episode_id": "ep1",
                    "podcast": "硅谷101",
                    "speaker": "节目简介",
                    "text": "Agent 进入商业化阶段",
                    "evidence": "简介提到投资与商业化。",
                    "source": "intro",
                    "confidence": "低",
                    "topic_hint": "商业化",
                }
            ],
            "subtopics": [
                {
                    "subtopic_id": "S01",
                    "title": "下一步战场",
                    "relation": "partial",
                    "positions": [
                        {
                            "episode_id": "ep1",
                            "summary": "偏向商业化和资本视角。",
                            "source": "intro",
                            "claim_ids": ["C01"],
                        },
                        {
                            "episode_id": "ep2",
                            "summary": "偏向软件范式和产品形态。",
                            "source": "audio",
                            "claim_ids": [],
                        },
                    ],
                    "note": "两期关心的问题互补。",
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            server.render_structured_comparison_files(project, "AI 下一步", selected, comparison)
            atoms = (project / "观点原子.md").read_text(encoding="utf-8")
            cross = (project / "跨集比对.md").read_text(encoding="utf-8")

        self.assertIn("Agent 进入商业化阶段", atoms)
        self.assertIn("观点矩阵", cross)
        self.assertIn("◐部分分歧", cross)
        self.assertIn("📝 简介依据", cross)
        self.assertIn("🎧 听稿依据", cross)


if __name__ == "__main__":
    unittest.main()
