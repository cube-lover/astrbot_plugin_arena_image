from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx


class ApiModelContractTest(unittest.IsolatedAsyncioTestCase):
    async def test_models_endpoint_exposes_image_capabilities(self) -> None:
        from src import constants, main

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "config.json"
            models_path = root / "models.json"
            config_path.write_text(
                json.dumps(
                    {
                        "api_keys": [
                            {
                                "name": "test",
                                "key": "test-key",
                                "rpm": 999,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            models_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "image-internal",
                            "publicName": "gpt-image-2 (medium)",
                            "organization": "openai",
                            "capabilities": {
                                "outputCapabilities": {"image": True},
                                "inputCapabilities": {"image": True},
                            },
                        },
                        {
                            "id": "text-internal",
                            "publicName": "text-model",
                            "organization": "test",
                            "capabilities": {
                                "outputCapabilities": {"text": True},
                            },
                        },
                    ]
                ),
                encoding="utf-8",
            )

            with (
                patch.object(main, "CONFIG_FILE", str(config_path)),
                patch.object(
                    constants,
                    "MODELS_FILE",
                    str(models_path),
                ),
            ):
                main.api_key_usage.clear()
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://test",
                ) as client:
                    response = await client.get(
                        "/api/v1/models",
                        headers={"Authorization": "Bearer test-key"},
                    )

            self.assertEqual(response.status_code, 200)
            data = response.json()["data"]
            image_model = next(item for item in data if item["id"] == "gpt-image-2 (medium)")
            self.assertTrue(image_model["output_image"])
            self.assertTrue(image_model["input_image"])
            self.assertEqual(image_model["owned_by"], "openai")

    async def test_image_attachment_uses_original_filename_not_r2_key(self) -> None:
        from src import main

        uploaded = ("user/123/opaque-object-key.jpg", "https://r2.example/signed")
        content = [
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/jpeg;base64,aGVsbG8=",
                },
            },
            {"type": "text", "text": "make it brighter"},
        ]

        async def fake_upload(image_data: bytes, mime_type: str, filename: str):
            self.assertEqual(mime_type, "image/jpeg")
            self.assertTrue(filename.startswith("upload-"))
            self.assertTrue(filename.endswith(".jpg"))
            return uploaded

        with patch.object(main, "upload_image_to_lmarena", fake_upload):
            prompt, attachments = await main.process_message_content(
                content,
                {"inputCapabilities": {"image": True}},
            )

        self.assertEqual(prompt, "make it brighter")
        self.assertEqual(
            attachments,
            [
                {
                    "name": attachments[0]["name"],
                    "contentType": "image/jpeg",
                    "url": uploaded[1],
                }
            ],
        )
        self.assertNotEqual(attachments[0]["name"], uploaded[0])


if __name__ == "__main__":
    unittest.main()
