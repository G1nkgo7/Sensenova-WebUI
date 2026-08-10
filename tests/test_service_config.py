import tempfile
import unittest
from pathlib import Path
from unittest import mock

from studio.app import custom_models, service_config


class ServiceConfigProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patches = [
            mock.patch.object(service_config, "CONFIG_DIR", root / "configs"),
            mock.patch.object(custom_models, "_KEY_FILE", root / ".key"),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

    def test_sensenova_provider_persists_and_reaches_job_env(self):
        service_config.update(
            7,
            image_enabled=True,
            image_provider="sensenova_u1",
            image_base_url="https://token.sensenova.cn/v1/",
            image_model="u1-model",
            image_api_key="secret",
            clear_image_api_key=False,
            search_enabled=False,
            search_base_url="",
            search_api_key=None,
            clear_search_api_key=False,
        )
        public = service_config.public_payload(7)["image_generation"]
        self.assertEqual(public["provider"], "sensenova_u1")
        self.assertTrue(public["has_api_key"])
        env = service_config.runtime_env(7)
        self.assertEqual(env["IMAGE_PROVIDER"], "sensenova_u1")
        self.assertEqual(env["IMAGE_BASE_URL"], "https://token.sensenova.cn/v1")
        self.assertEqual(env["IMAGE_MODEL"], "u1-model")
        self.assertEqual(env["IMAGE_API_KEY"], "secret")

    def test_unknown_provider_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "不支持的生图服务类型"):
            service_config.update(
                7,
                image_enabled=False,
                image_provider="unknown",
                image_base_url="",
                image_model="",
                image_api_key=None,
                clear_image_api_key=False,
                search_enabled=False,
                search_base_url="",
                search_api_key=None,
                clear_search_api_key=False,
            )


if __name__ == "__main__":
    unittest.main()
