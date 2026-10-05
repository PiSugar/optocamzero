import json
import os
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from magic_service import (  # noqa: E402
    MagicGenerationWorker,
    MagicModeStore,
    OpenAIImageEditor,
    is_ai_generated,
    mask_api_key,
    normalize_magic_modes,
    read_magic_source_metadata,
    save_magic_connection_settings,
)


class MagicServiceTests(unittest.TestCase):
    def test_normalize_modes_deduplicates_ids_and_drops_incomplete_entries(self):
        modes = normalize_magic_modes([
            {"title": "Cheese!", "prompt": "One"},
            {"title": "Cheese!", "prompt": "Two"},
            {"title": "", "prompt": "Missing title"},
        ])
        self.assertEqual([mode["id"] for mode in modes], ["cheese", "cheese-2"])

    def test_store_seeds_cheese_and_saves_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MagicModeStore(Path(directory) / "magic_modes.json")
            self.assertEqual(store.load()[0]["id"], "cheese")
            saved = store.save([{"title": "Clay", "prompt": "Turn it into clay"}])
            self.assertEqual(saved[0]["id"], "clay")

    def test_connection_settings_mask_key_preserve_blank_and_save_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("# keep\nOPTOCAM_MAGIC_MODEL=test-model\nOPENAI_API_KEY=sk-oldsecret1234\n")
            settings = save_magic_connection_settings(
                env_path, api_key="", proxy="http://127.0.0.1:7890"
            )
            self.assertTrue(settings["api_key_configured"])
            self.assertEqual(settings["api_key_masked"], mask_api_key("sk-oldsecret1234"))
            self.assertEqual(settings["api_key_masked"], "********")
            self.assertNotIn("api_key", settings)
            self.assertNotIn("sk-oldsecret1234", json.dumps(settings))
            self.assertEqual(settings["proxy"], "http://127.0.0.1:7890")
            self.assertIn("OPTOCAM_MAGIC_MODEL=test-model", env_path.read_text())
            self.assertEqual(os.stat(env_path).st_mode & 0o777, 0o600)

            cleared = save_magic_connection_settings(env_path, clear_api_key=True)
            self.assertFalse(cleared["api_key_configured"])
            self.assertNotIn("sk-oldsecret1234", env_path.read_text())

    def test_connection_settings_reject_non_http_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                save_magic_connection_settings(Path(directory) / ".env", proxy="socks5://localhost:1080")

    def test_image_editor_defaults_to_square_output(self):
        with patch.dict(os.environ, {"OPTOCAM_MAGIC_SIZE": ""}):
            self.assertEqual(OpenAIImageEditor().size, "1024x1024")

    @unittest.skipIf(PILImage is None, "Pillow is not installed")
    def test_image_editor_compresses_upload_source_to_512_square(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.jpg"
            PILImage.new("RGB", (900, 600), (120, 80, 40)).save(source, "JPEG", quality=95)
            compressed = OpenAIImageEditor._prepare_source_image(source)
            with PILImage.open(BytesIO(compressed)) as image:
                self.assertEqual(image.size, (512, 512))
                self.assertEqual(image.format, "JPEG")
            with PILImage.open(source) as original:
                self.assertEqual(original.size, (900, 600))

    def test_worker_writes_generated_photo_and_ai_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "photos").mkdir()
            source = home / "photos" / "Optocamzero_1.jpg"
            source.write_bytes(b"source")
            ready = []
            worker = MagicGenerationWorker(
                home,
                lambda: "Optocamzero_2.jpg",
                on_ready=lambda path, job: ready.append((path, job)),
            )
            self.assertTrue(worker.enqueue(source, "cheese"))
            source_state = read_magic_source_metadata(source)
            self.assertEqual(source_state["status"], "queued")
            self.assertEqual(source_state["magic_mode_title"], "Cheese")
            job_path = next((home / "magic_queue").glob("*.json"))
            job = json.loads(job_path.read_text())
            jpeg = b"\xff\xd8" + (b"x" * 1200) + b"\xff\xd9"
            with patch.object(OpenAIImageEditor, "edit", return_value=jpeg):
                worker._process(job_path, job)
            output = home / "photos" / "Optocamzero_2.jpg"
            self.assertEqual(output.read_bytes(), jpeg)
            self.assertTrue(is_ai_generated(output))
            self.assertEqual(json.loads(Path(str(output) + ".ai.json").read_text())["source"], source.name)
            source_state = read_magic_source_metadata(source)
            self.assertEqual(source_state["status"], "complete")
            self.assertEqual(source_state["output"], output.name)
            self.assertEqual(len(ready), 1)

    def test_worker_hot_loads_key_and_proxy_for_each_job(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "photos").mkdir()
            source = home / "photos" / "Optocamzero_1.jpg"
            source.write_bytes(b"source")
            save_magic_connection_settings(
                home / ".env", api_key="sk-live123456", proxy="http://proxy.local:8080"
            )
            worker = MagicGenerationWorker(home, lambda: "Optocamzero_2.jpg")
            worker.enqueue(source, "cheese")
            job_path = next((home / "magic_queue").glob("*.json"))
            job = json.loads(job_path.read_text())
            observed = {}
            jpeg = b"\xff\xd8" + (b"x" * 1200) + b"\xff\xd9"

            def fake_edit(editor, _source, _prompt):
                observed["api_key"] = editor.api_key
                observed["proxy"] = editor.proxy
                return jpeg

            with patch.dict(os.environ, {}, clear=False), patch.object(OpenAIImageEditor, "edit", fake_edit):
                worker._process(job_path, job)
            self.assertEqual(observed["api_key"], "sk-live123456")
            self.assertEqual(observed["proxy"], "http://proxy.local:8080")


if __name__ == "__main__":
    unittest.main()
