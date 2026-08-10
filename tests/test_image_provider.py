import base64
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOLS_PATH = ROOT / "bundled/static-ppt-skill-suite/harnesses/long-horizon-presenter/core/tools.py"
SPEC = importlib.util.spec_from_file_location("presenter_tools_for_test", TOOLS_PATH)
TOOLS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(TOOLS)


class FakeResponse:
    def __init__(self, payload, content=b""):
        self._payload = payload
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class ImageProviderTests(unittest.TestCase):
    def test_openai_images_request(self):
        agent = SimpleNamespace(
            image_provider="openai_images", img_base="https://image.test/v1",
            img_key="secret", image_model="image-model",
        )
        response = FakeResponse({"data": [{"b64_json": base64.b64encode(b"png").decode()}]})
        with mock.patch.object(TOOLS.requests, "post", return_value=response) as post:
            payload = TOOLS._image_api_request(agent, "draw it", "1536x1024")
        self.assertEqual(TOOLS._image_bytes_from_payload(payload), b"png")
        self.assertEqual(post.call_args.args[0], "https://image.test/v1/images/generations")
        self.assertEqual(post.call_args.kwargs["json"]["size"], "1536x1024")
        self.assertEqual(post.call_args.kwargs["timeout"], 600)

    def test_sensenova_u1_images_request(self):
        agent = SimpleNamespace(
            image_provider="sensenova_u1", img_base="https://token.sensenova.cn/v1",
            img_key="secret", image_model="sensenova-u1-fast",
        )
        response = FakeResponse({"data": [{"url": "https://cdn.test/u1.png"}]})
        with mock.patch.object(TOOLS.requests, "post", return_value=response) as post:
            payload = TOOLS._image_api_request(agent, "draw it", "1536x1024", "landscape")
        self.assertEqual(payload["data"][0]["url"], "https://cdn.test/u1.png")
        self.assertEqual(post.call_args.args[0], "https://token.sensenova.cn/v1/images/generations")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["prompt"], "draw it")
        self.assertEqual(body["size"], "2752x1536")
        self.assertEqual(body["response_format"], "url")
        self.assertEqual(body["output_format"], "png")
        self.assertEqual(post.call_args.kwargs["timeout"], 600)

    def test_sensenova_message_images_url(self):
        payload = {"choices": [{"message": {"images": [{"url": "https://cdn.test/out.webp"}]}}]}
        with mock.patch.object(TOOLS.requests, "get", return_value=FakeResponse({}, b"downloaded")):
            self.assertEqual(TOOLS._image_bytes_from_payload(payload), b"downloaded")


if __name__ == "__main__":
    unittest.main()
