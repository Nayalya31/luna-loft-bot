import base64
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.proxyapi.ru/openai/v1"
DEFAULT_IMAGE_MODEL = "gpt-image-2"


class ProxyAPIError(Exception):
    """Ошибка при обращении к ProxyAPI."""


class ProxyAPIClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_IMAGE_MODEL,
    ) -> None:
        if not api_key:
            raise ValueError("Укажите ключ ProxyAPI в .env (переменная PROXY_API)")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _format_http_error(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
            message = payload.get("error", payload)
            if isinstance(message, dict):
                message = message.get("message") or message
            return f"{response.status_code} {response.reason_phrase}: {message}"
        except ValueError:
            return f"{response.status_code} {response.reason_phrase}: {response.text[:300]}"

    def _decode_image(self, data: dict[str, Any]) -> bytes:
        try:
            b64_json = data["data"][0]["b64_json"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProxyAPIError("ProxyAPI не вернул изображение") from exc
        return base64.b64decode(b64_json)

    def generate_image(
        self,
        prompt: str,
        size: str = "1024x1024",
        quality: str = "medium",
    ) -> bytes:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "quality": quality,
        }

        try:
            response = httpx.post(
                f"{self.base_url}/images/generations",
                headers=self._headers(),
                json=payload,
                timeout=180.0,
            )
            if response.is_error:
                raise ProxyAPIError(
                    "Не удалось сгенерировать изображение. "
                    f"{self._format_http_error(response)}"
                )
            return self._decode_image(response.json())
        except httpx.HTTPError as exc:
            raise ProxyAPIError(f"Не удалось сгенерировать изображение: {exc}") from exc

    def edit_image(self, prompt: str, image_bytes: bytes, filename: str = "image.png") -> bytes:
        files = [
            ("model", (None, self.model)),
            ("image[]", (filename, image_bytes, "application/octet-stream")),
            ("prompt", (None, prompt)),
        ]

        try:
            response = httpx.post(
                f"{self.base_url}/images/edits",
                headers={"Authorization": f"Bearer {self.api_key}"},
                files=files,
                timeout=180.0,
            )
            if response.is_error:
                raise ProxyAPIError(
                    "Не удалось отредактировать изображение. "
                    f"{self._format_http_error(response)}"
                )
            return self._decode_image(response.json())
        except httpx.HTTPError as exc:
            raise ProxyAPIError(f"Не удалось отредактировать изображение: {exc}") from exc
