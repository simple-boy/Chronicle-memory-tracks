"""Deterministic capacity errors must occur before any paid indexing attempt."""
import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from coding import CodingProcessor
from models import ModelClient
from multimodal import MultimodalProcessor
from records import PayloadTooLarge
from store import TrackStore


def picture(color):
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(stream, "PNG")
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," +
            base64.b64encode(stream.getvalue()).decode()}}


class CapacityTests(unittest.TestCase):
    def test_over_budget_code_rejects_before_network_and_retry_is_free(self):
        model = ModelClient(mode="openai", api_key="synthetic-not-a-key", max_calls_per_add=2)
        with tempfile.TemporaryDirectory() as tmp, patch("models.urllib.request.urlopen") as network:
            store = TrackStore(str(Path(tmp) / "code.sqlite3"), CodingProcessor(model, chunk_chars=4))
            try:
                payload = dict(request_id="r", user_id="u", session_id="s",
                               messages=[{"role":"user", "content":"abcdefghijk"}])
                for _ in range(2):
                    with self.assertRaises(PayloadTooLarge):
                        store.add_messages(**payload)
                network.assert_not_called()
                self.assertEqual(store._connection().execute("SELECT COUNT(*) FROM memories").fetchone()[0], 0)
            finally:
                store.close()

    def test_over_budget_images_reject_before_network(self):
        model = ModelClient(mode="openai", api_key="synthetic-not-a-key", max_calls_per_add=2)
        with tempfile.TemporaryDirectory() as tmp, patch("models.urllib.request.urlopen") as network:
            store = TrackStore(str(Path(tmp) / "vision.sqlite3"), MultimodalProcessor(model))
            try:
                with self.assertRaises(PayloadTooLarge):
                    store.add_messages(request_id="r",user_id="u",session_id="s",
                                       messages=[{"role":"user","content":[picture(c) for c in ("red","blue","green")]}])
                network.assert_not_called()
            finally:
                store.close()

    def test_disabled_visual_features_do_not_use_exact_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TrackStore(str(Path(tmp) / "vision.sqlite3"),
                               MultimodalProcessor(image_features=False), temporal=False)
            try:
                red = picture("red")
                store.add_messages(request_id="r",user_id="u",session_id="s",messages=[
                    {"role":"user","content":[red]}, {"role":"user","content":[picture("blue")]}])
                output = store.search(user_id="u", query=[red], top_k=2)
                self.assertTrue(output)
                # No image score or exact-file bonus: remaining scores are lexical ranks.
                self.assertTrue(all(row["score"] <= 1 for row in output))
            finally:
                store.close()
