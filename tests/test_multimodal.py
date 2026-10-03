"""No network or model calls: ordered evidence, media validation, visual retrieval."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import unittest
from unittest.mock import patch

from PIL import Image

import multimodal
from multimodal import MultimodalProcessor
from records import PayloadTooLarge, ProviderError


def image_uri(color="red", *, size=(16, 16), file_format="PNG"):
    buffer = io.BytesIO()
    with Image.new("RGB", size, color) as image:
        image.save(buffer, format=file_format)
    data = buffer.getvalue()
    mime = {"JPEG": "jpeg", "PNG": "png", "WEBP": "webp"}[file_format]
    return f"data:image/{mime};base64," + base64.b64encode(data).decode("ascii"), data


def image_part(uri):
    return {"type": "image_url", "image_url": {"url": uri}}


class FakeProvider:
    def __init__(self):
        self.image_calls = []
        self.text_calls = []

    def describe_image(self, data_uri):
        self.image_calls.append(data_uri)
        return "derived description for indexing only"

    def index_text(self, text):
        self.text_calls.append(text)
        return ["derived text key"]


class MultimodalTests(unittest.TestCase):
    def test_original_parts_order_and_image_bytes_are_preserved(self):
        first, first_bytes = image_uri("red")
        second, _ = image_uri("blue", file_format="JPEG")
        source = [image_part(first), {"type": "text", "text": "  first source\n"},
                  image_part(second), {"type": "text", "text": "second source"}]
        original_json = json.dumps(source)
        provider = FakeProvider()
        evidence, = MultimodalProcessor(provider).prepare(source)
        self.assertEqual(json.dumps(evidence.content), original_json)
        self.assertEqual(evidence.content[0]["image_url"]["url"], first)
        self.assertEqual(evidence.features["images"][0]["sha256"], hashlib.sha256(first_bytes).hexdigest())
        self.assertIn("derived description", evidence.index_text)
        self.assertNotIn("derived description", json.dumps(evidence.content))
        self.assertEqual(provider.image_calls, [first, second])
        source[0]["image_url"]["url"] = "modified after preparation"
        self.assertEqual(evidence.content[0]["image_url"]["url"], first)

    def test_query_images_never_call_provider_and_color_changes_rank(self):
        query_uri, _ = image_uri("red", size=(11, 13))
        red_uri, _ = image_uri("red", size=(21, 17))
        blue_uri, _ = image_uri("blue", size=(21, 17))
        provider = FakeProvider()
        processor = MultimodalProcessor(provider)
        red, = processor.prepare([image_part(red_uri)])
        blue, = processor.prepare([image_part(blue_uri)])
        calls_before = list(provider.image_calls)
        query = processor.query([image_part(query_uri)], options=["A. red", "B. blue"])
        self.assertEqual(provider.image_calls, calls_before)
        self.assertEqual(provider.text_calls, [])
        self.assertIn(multimodal.IMAGE_CANDIDATE_MARKER, query.text)
        self.assertIn("A. red", query.text)
        self.assertGreater(processor.score(query.features, red.features),
                           processor.score(query.features, blue.features))
        exact = processor.query([image_part(red_uri)])
        self.assertEqual(processor.score(exact.features, red.features), 1.0)
        self.assertEqual(processor.score(query.features, {"images": []}), 0.0)

    def test_image_only_local_index_is_nonsemantic(self):
        uri, _ = image_uri("red")
        processor = MultimodalProcessor()
        evidence, = processor.prepare([image_part(uri)])
        self.assertEqual(evidence.index_text, multimodal.IMAGE_CANDIDATE_MARKER)
        self.assertEqual(processor.query([image_part(uri)]).text, multimodal.IMAGE_CANDIDATE_MARKER)

    def test_strings_and_text_only_arrays_preserve_source(self):
        processor = MultimodalProcessor()
        source = "  original text\nwith newlines  "
        evidence, = processor.prepare(source)
        self.assertEqual(evidence.content, source)
        self.assertEqual(evidence.index_text, source)
        self.assertEqual(evidence.features, {"images": []})
        self.assertEqual(processor.media_bytes(source), 0)
        self.assertEqual(processor.query(source).text, source)
        provider = FakeProvider()
        processor = MultimodalProcessor(provider)
        parts = [{"type": "text", "text": "part one"}, {"type": "text", "text": "part two"}]
        evidence, = processor.prepare(parts)
        self.assertEqual(evidence.content, parts)
        self.assertIn("derived text key", evidence.index_text)
        self.assertEqual(provider.text_calls, ["part one\npart two"])
        processor.query("a query")
        self.assertEqual(provider.text_calls, ["part one\npart two"])

    def test_bad_schema_remote_base64_and_mime_are_rejected(self):
        png, _ = image_uri()
        invalid = [
            "", " \n", [], {}, [{"type": "text", "text": " "}],
            [{"type": "audio_url", "audio_url": {"url": "data:audio/wav;base64,AA=="}}],
            ["not an object"], [{"type": "image_url", "image_url": {}}],
            [image_part("https://example.com/image.png")],
            [image_part("data:image/png;base64,%%%%")],
            [image_part("data:image/png;base64,A")],
            [image_part("data:image/gif;base64,AAAA")],
            [image_part("data:image/png;base64," + base64.b64encode(b"not an image").decode())],
            [image_part(png.replace("image/png", "image/jpeg"))],
        ]
        for content in invalid:
            with self.subTest(content=str(content)[:80]), self.assertRaises(ValueError):
                MultimodalProcessor().prepare(content)
        with self.assertRaises(ValueError):
            MultimodalProcessor().query("query", options=[1])

    def test_all_parts_validate_before_provider_is_called(self):
        uri, _ = image_uri()
        provider = FakeProvider()
        with self.assertRaises(ValueError):
            MultimodalProcessor(provider).prepare([image_part(uri), image_part("https://example.com/x")])
        self.assertEqual(provider.image_calls, [])

    def test_media_bytes_count_decoded_files_not_base64_length(self):
        first, first_bytes = image_uri()
        second, second_bytes = image_uri("blue", file_format="WEBP")
        processor = MultimodalProcessor()
        self.assertEqual(processor.media_bytes([image_part(first), image_part(second)]),
                         len(first_bytes) + len(second_bytes))
        with patch.object(multimodal, "MAX_IMAGE_BYTES", len(first_bytes)):
            self.assertEqual(processor.media_bytes([image_part(first)]), len(first_bytes))
        with patch.object(multimodal, "MAX_IMAGE_BYTES", len(first_bytes) - 1):
            with self.assertRaises(PayloadTooLarge):
                processor.prepare([image_part(first)])

    def test_actual_ten_mib_boundary_rejects_oversize_before_image_decoding(self):
        encoded = base64.b64encode(b"x" * (10 * 1024 * 1024 + 1)).decode("ascii")
        uri = "data:image/png;base64," + encoded
        with patch.object(multimodal.Image, "open", side_effect=AssertionError("must reject before Pillow")):
            with self.assertRaises(PayloadTooLarge):
                MultimodalProcessor().prepare([image_part(uri)])

    def test_capacity_pixel_limit_is_configurable_and_frozen(self):
        uri, _ = image_uri(size=(8, 8))
        processor = MultimodalProcessor(max_pixels=63)
        with self.assertRaises(PayloadTooLarge):
            processor.prepare([image_part(uri)])
        self.assertEqual(MultimodalProcessor(max_pixels=64).media_bytes([image_part(uri)]),
                         len(base64.b64decode(uri.split(",", 1)[1])))
        self.assertEqual(MultimodalProcessor().config_dict()["max_image_pixels"], 16_000_000)
        self.assertEqual(processor.config_dict()["max_image_pixels"], 63)
        for invalid in (0, -1, True, 1.0):
            with self.assertRaises(ValueError):
                MultimodalProcessor(max_pixels=invalid)

    def test_disabling_features_keeps_raw_images_and_media_validation(self):
        uri, _ = image_uri()
        processor = MultimodalProcessor(image_features=False)
        evidence, = processor.prepare([image_part(uri)])
        query = processor.query([image_part(uri)])
        self.assertEqual(evidence.content, [image_part(uri)])
        self.assertNotIn("dhash", evidence.features["images"][0])
        self.assertEqual(processor.score(query.features, evidence.features), 0.0)

    def test_empty_provider_caption_fails_the_add(self):
        uri, _ = image_uri()
        provider = FakeProvider()
        provider.describe_image = lambda _: " "
        with self.assertRaises(ProviderError):
            MultimodalProcessor(provider).prepare([image_part(uri)])

    def test_disabled_model_is_never_called(self):
        uri, _ = image_uri()
        provider = FakeProvider()
        provider.enabled = False
        processor = MultimodalProcessor(provider)
        processor.prepare([image_part(uri)])
        processor.prepare("text source")
        processor.query([image_part(uri)])
        self.assertEqual(provider.image_calls, [])
        self.assertEqual(provider.text_calls, [])
        self.assertFalse(processor.config_dict()["model_enabled"])


if __name__ == "__main__":
    unittest.main()
