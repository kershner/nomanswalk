import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
import os
import tempfile
from unittest.mock import Mock, patch

import nms_bluesky
import nms_bluesky_tags


class BlueskyTagTests(unittest.TestCase):
    def test_pick_tags_uses_core_and_three_rotating_tags(self):
        with (
            patch(
                "nms_bluesky_tags.active_rotating_tags",
                return_value=["exploration", "scifi", "gaming", "twitchclips"],
            ),
            patch(
                "nms_bluesky_tags.random.sample",
                return_value=["exploration", "scifi", "twitchclips"],
            ),
        ):
            tags = nms_bluesky._pick_tags()

        self.assertEqual(tags[:3], list(nms_bluesky_tags.CORE_TAGS))
        self.assertEqual(tags[3:], ["exploration", "scifi", "twitchclips"])
        self.assertEqual(len(tags), 6)
        self.assertEqual(len(tags), len(set(tags)))

    def test_nanosecond_timestamp_is_accepted(self):
        created_at = nms_bluesky_tags._created_at(
            {"record": {"createdAt": "2026-09-07T22:06:36.969953734Z"}}
        )
        self.assertEqual(
            created_at,
            datetime(2026, 9, 7, 22, 6, 36, 969953, tzinfo=timezone.utc),
        )

    def test_pool_becomes_stale_after_seven_days(self):
        now = datetime(2026, 9, 11, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            pool_file = os.path.join(directory, "pool.json")
            with open(pool_file, "w", encoding="utf-8") as handle:
                handle.write('{"updated_at": "2026-09-05T00:00:00Z"}')
            self.assertFalse(nms_bluesky_tags.pool_is_stale(now, pool_file))
            self.assertTrue(
                nms_bluesky_tags.pool_is_stale(now + timedelta(days=2), pool_file)
            )

    def test_refresh_promotes_discovered_tag_and_saves_pool(self):
        now = datetime(2026, 9, 11, tzinfo=timezone.utc)

        def metrics(_client, tag, _own_did, _now):
            engagement = 12 if tag == "promisingtag" else 3
            return {
                "posts_7d": 20,
                "posts_30d": 50,
                "authors_30d": 25,
                "top_author_share": 0.1,
                "median_engagement": engagement,
                "p75_engagement": engagement * 2,
            }

        with tempfile.TemporaryDirectory() as directory:
            pool_file = os.path.join(directory, "pool.json")
            with (
                patch.object(
                    nms_bluesky_tags,
                    "_discover_candidates",
                    return_value=Counter({"promisingtag": 12}),
                ),
                patch.object(
                    nms_bluesky_tags,
                    "_audit_candidate",
                    side_effect=metrics,
                ),
            ):
                payload = nms_bluesky_tags.refresh_tag_pool(
                    Mock(), "did:example:own", now, pool_file
                )

            self.assertEqual(payload["tags"][0]["tag"], "promisingtag")
            self.assertEqual(len(payload["tags"]), nms_bluesky_tags.MAX_POOL_SIZE)
            self.assertEqual(nms_bluesky_tags._load_pool(pool_file), payload)

    def test_bad_audit_does_not_replace_existing_pool(self):
        bad_metrics = {
            "posts_7d": 1,
            "posts_30d": 2,
            "authors_30d": 1,
            "top_author_share": 1.0,
            "median_engagement": 0,
            "p75_engagement": 0,
        }
        with tempfile.TemporaryDirectory() as directory:
            pool_file = os.path.join(directory, "pool.json")
            with open(pool_file, "w", encoding="utf-8") as handle:
                handle.write("keep me")
            with (
                patch.object(
                    nms_bluesky_tags,
                    "_discover_candidates",
                    return_value=Counter(),
                ),
                patch.object(
                    nms_bluesky_tags,
                    "_audit_candidate",
                    return_value=bad_metrics,
                ),
            ):
                with self.assertRaises(RuntimeError):
                    nms_bluesky_tags.refresh_tag_pool(
                        Mock(), "did:example:own", pool_file=pool_file
                    )
            with open(pool_file, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "keep me")


if __name__ == "__main__":
    unittest.main()
