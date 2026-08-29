import unittest
from unittest.mock import Mock, patch

from atproto_client.models.blob_ref import BlobRef, IpldLink

import nms_bluesky
import nms_threads
from nms_twitch_bot import _build_selfie_caption


class BlueskyBlobUrlTests(unittest.TestCase):
    def test_original_blob_url_contains_encoded_did_and_cid(self):
        client = Mock()
        client.me.did = "did:plc:example"
        blob = BlobRef(
            mimeType="image/jpeg",
            size=123,
            ref=IpldLink(**{"$link": "bafkreiexample"}),
        )
        self.assertEqual(
            nms_bluesky._blob_url(client, blob),
            "https://bsky.social/xrpc/com.atproto.sync.getBlob"
            "?did=did%3Aplc%3Aexample&cid=bafkreiexample",
        )

    def test_selfie_viewer_url_links_to_twitch(self):
        text = "Selfie requested by Twitch viewer twitch.tv/billcrystals"
        facet = nms_bluesky._link_facets(text)[0]
        self.assertEqual(
            facet["features"][0]["uri"],
            "https://www.twitch.tv/billcrystals",
        )


class SelfieCaptionTests(unittest.TestCase):
    def test_caption_uses_two_paragraphs_and_bullets(self):
        caption = _build_selfie_caption(
            "billcrystals",
            {
                "planet": {
                    "name": "Deran 32/S9",
                    "biome": "Scorched",
                    "planet_size": "Medium",
                    "weather_type": "Scorched",
                },
                "universe_address": {"galaxy_number": 22},
            },
        )

        self.assertTrue(caption.startswith("Greetings from Deran 32/S9!\n\n"))
        self.assertIn("Selfie requested by Twitch viewer twitch.tv/billcrystals • Galaxy:", caption)
        self.assertIn(" • Biome: Scorched • Size: Medium • Weather: Scorched", caption)
        self.assertTrue(caption.endswith(" • 🔴twitch.tv/nomanswalk"))

class ThreadsPublishingTests(unittest.TestCase):
    @patch("nms_threads._request")
    @patch("nms_threads._fresh_tokens")
    def test_image_create_process_publish_and_permalink(self, fresh, request):
        fresh.return_value = {"access_token": "token", "user_id": "123"}
        request.side_effect = [
            {"id": "container"},
            {"status": "FINISHED"},
            {"id": "media"},
            {"id": "media", "permalink": "https://threads.example/post"},
        ]

        result = nms_threads.post_media(
            "https://bsky.social/original.jpg",
            "IMAGE",
            "A new planet",
        )

        self.assertEqual(result, "https://threads.example/post")
        create_data = request.call_args_list[0].kwargs["data"]
        self.assertEqual(create_data["image_url"], "https://bsky.social/original.jpg")
        self.assertEqual(create_data["topic_tag"], "No Man's Sky")
        self.assertEqual(request.call_args_list[2].kwargs["data"]["creation_id"], "container")

    def test_media_url_must_use_https(self):
        with self.assertRaises(ValueError):
            nms_threads.post_media("http://localhost/image.jpg", "IMAGE", "caption")



if __name__ == "__main__":
    unittest.main()
