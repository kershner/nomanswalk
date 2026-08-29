import unittest
from unittest.mock import patch

import nms_bluesky


class BlueskyTagTests(unittest.TestCase):
    @patch("nms_bluesky.random.choice", side_effect=lambda options: options[0])
    def test_pick_tags_covers_each_category_and_wildcard(self, _choice):
        tags = nms_bluesky._pick_tags()

        self.assertEqual(tags[:2], ["NoMansSky", "nms"])
        self.assertEqual(len(tags), 7)
        self.assertEqual(len(tags), len(set(tags)))
        for tag, group in zip(tags[2:6], nms_bluesky.TAG_GROUPS):
            self.assertIn(tag, group)
        self.assertIn(tags[6], nms_bluesky.TAGS_POOL)


if __name__ == "__main__":
    unittest.main()
