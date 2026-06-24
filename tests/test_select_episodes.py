import unittest

import server


class SelectEpisodesTest(unittest.TestCase):
    def test_selects_recent_and_hot_candidates(self):
        candidates = [
            {
                "title": "old",
                "podcast": "A",
                "date": "2024-01-01",
                "play_count": 1000,
            },
            {
                "title": "recent",
                "podcast": "B",
                "date": "2026-06-10",
                "play_count": 5000,
            },
            {
                "title": "hot",
                "podcast": "C",
                "date": "2026-05-20",
                "play_count": "10万",
            },
        ]

        selected = server.select_episodes(candidates, 2, "90")

        self.assertEqual([item["title"] for item in selected], ["hot", "recent"])

    def test_limits_same_podcast_before_backfilling(self):
        candidates = [
            {"title": "a1", "podcast": "A", "date": "2026-06-18", "play_count": 1000},
            {"title": "a2", "podcast": "A", "date": "2026-06-17", "play_count": 1000},
            {"title": "a3", "podcast": "A", "date": "2026-06-16", "play_count": 1000},
            {"title": "b1", "podcast": "B", "date": "2026-06-01", "play_count": 1000},
        ]

        selected = server.select_episodes(candidates, 3, "30", max_per_podcast=2)

        self.assertEqual([item["title"] for item in selected], ["a1", "a2", "b1"])

    def test_backfills_when_all_candidates_are_from_same_podcast(self):
        candidates = [
            {"title": "a1", "podcast": "A", "date": "2026-06-18"},
            {"title": "a2", "podcast": "A", "date": "2026-06-17"},
            {"title": "a3", "podcast": "A", "date": "2026-06-16"},
        ]

        selected = server.select_episodes(candidates, 3, "30", max_per_podcast=2)

        self.assertEqual([item["title"] for item in selected], ["a1", "a2", "a3"])


if __name__ == "__main__":
    unittest.main()
