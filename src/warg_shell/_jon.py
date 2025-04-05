import json
import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Union

import httpx


@dataclass(frozen=True)
class ShellUrlResponse:
    success: bool
    error: str = ""
    url: str = ""


@dataclass(frozen=True)
class PgDumpResponse:
    success: bool
    error: str | dict = ""


@dataclass
class Jon:
    base_domain: str
    client: httpx.AsyncClient = field(init=False)

    def __post_init__(self):
        self.client = httpx.AsyncClient(base_url=self.base_url)

    @property
    def base_url(self) -> httpx.URL:
        if not re.match(r"^https?://", self.base_domain):
            origin = f"https://{self.base_domain}"
        else:
            origin = self.base_domain

        return httpx.URL(origin).join("/back/api/warg/")

    async def get_auth_token(self, req_token: str) -> dict:
        resp = await self.client.post("token/", json=dict(token=req_token))
        resp.raise_for_status()

        return resp.json()

    async def get_shell_url(
        self,
        token: str,
        product: str,
        env: str,
        component: str,
    ) -> ShellUrlResponse:
        resp = await self.client.post(
            "shell/",
            json=dict(
                token=token,
                product=product,
                env=env,
                component=component,
            ),
        )

        if resp.status_code in [200, 404]:
            data = resp.json()

            if resp.status_code == 200:
                return ShellUrlResponse(success=True, url=data["url"])
            else:
                return ShellUrlResponse(success=False, error=data["detail"])
        else:
            resp.raise_for_status()

    async def get_pg_dump(
        self, token: str, product: str, env: str, db: str
    ) -> AsyncGenerator[Union[PgDumpResponse, bytes], None]:
        async with self.client.stream(
            "POST",
            "pg_dump/",
            json=dict(
                token=token,
                product=product,
                env=env,
                db=db,
            ),
        ) as r:
            if r.status_code in [400, 404]:
                error_content = await r.aread()
                error_json = json.loads(error_content)
                yield PgDumpResponse(success=False, error=error_json["error"])
                return
            elif r.status_code >= 400:
                r.raise_for_status()

            async for chunk in r.aiter_bytes():
                yield chunk
