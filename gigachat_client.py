import base64
import time
import uuid
from typing import Any

import httpx

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_BASE_URL = "https://api.giga.chat"


class GigaChatError(Exception):
    """Ошибка при обращении к GigaChat API."""


def build_auth_key(client_id: str, client_secret: str) -> str:
    credentials = f"{client_id.strip()}:{client_secret.strip()}"
    return base64.b64encode(credentials.encode("utf-8")).decode("ascii")


def resolve_auth_key(
    auth_key: str = "",
    client_id: str = "",
    client_secret: str = "",
) -> str:
    if auth_key:
        return auth_key.removeprefix("Basic ").strip()
    if client_id and client_secret:
        return build_auth_key(client_id, client_secret)
    raise ValueError(
        "Укажите GIGACHAT_AUTH_KEY или пару GIGACHAT_CLIENT_ID + GIGACHAT_CLIENT_SECRET в .env"
    )


class GigaChatClient:
    def __init__(
        self,
        auth_key: str = "",
        client_id: str = "",
        client_secret: str = "",
        model: str = "GigaChat-2",
        verify_ssl: bool = False,
    ) -> None:
        self.auth_key = resolve_auth_key(auth_key, client_id, client_secret)
        self.model = model
        self.verify_ssl = verify_ssl
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    def _authorization_header(self) -> str:
        return f"Basic {self.auth_key}"

    def _format_http_error(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
            message = payload.get("message") or payload.get("error_description") or payload
            return f"{response.status_code} {response.reason_phrase}: {message}"
        except ValueError:
            return f"{response.status_code} {response.reason_phrase}: {response.text[:300]}"

    def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        try:
            response = httpx.post(
                OAUTH_URL,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "RqUID": str(uuid.uuid4()),
                    "Authorization": self._authorization_header(),
                },
                data={"scope": "GIGACHAT_API_PERS"},
                verify=self.verify_ssl,
                timeout=30.0,
            )
            if response.is_error:
                raise GigaChatError(
                    "Не удалось получить access token. "
                    f"{self._format_http_error(response)}"
                )
            data = response.json()
        except httpx.HTTPError as exc:
            raise GigaChatError(f"Не удалось получить access token: {exc}") from exc

        access_token = data.get("access_token")
        if not access_token:
            raise GigaChatError("GigaChat не вернул access token")

        expires_at = data.get("expires_at", 0)
        if expires_at > 1_000_000_000_000:
            expires_at /= 1000

        self._access_token = access_token
        self._token_expires_at = float(expires_at) if expires_at else time.time() + 1800
        return access_token

    def ask(
        self,
        user_message: str,
        system_prompt: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> str:
        token = self._get_access_token()
        messages: list[dict[str, str]] = []

        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }

        try:
            response = httpx.post(
                f"{API_BASE_URL}/v1/chat/completions",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                verify=self.verify_ssl,
                timeout=120.0,
            )
            if response.is_error:
                raise GigaChatError(
                    "Ошибка генерации ответа. "
                    f"{self._format_http_error(response)}"
                )
            data = response.json()
        except httpx.HTTPError as exc:
            raise GigaChatError(f"Ошибка генерации ответа: {exc}") from exc

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise GigaChatError("Неожиданный формат ответа GigaChat") from exc
