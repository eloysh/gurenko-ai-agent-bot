import httpx
from typing import Any, Dict, Optional

FREEPIK_BASE = "https://api.freepik.com"

class FreepikClient:
    def __init__(self, api_key: str, timeout: float = 60.0):
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        # Freepik auth header is x-freepik-api-key :contentReference[oaicite:4]{index=4}
        return {
            "x-freepik-api-key": self.api_key,
            "content-type": "application/json",
            "accept": "application/json",
        }

    async def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{FREEPIK_BASE}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(url, headers=self._headers(), json=payload)
            r.raise_for_status()
            return r.json()

    # --------- Image (Text->Image) ----------
    async def text_to_image_flux_dev(self, prompt: str, webhook_url: str, **kwargs) -> Dict[str, Any]:
        # /v1/ai/text-to-image/flux-dev :contentReference[oaicite:5]{index=5}
        payload = {
            "prompt": prompt,
            "webhook_url": webhook_url,
            **kwargs,
        }
        return await self.post("/v1/ai/text-to-image/flux-dev", payload)

    async def text_to_image_hyperflux(self, prompt: str, webhook_url: str, **kwargs) -> Dict[str, Any]:
        # /v1/ai/text-to-image/hyperflux :contentReference[oaicite:6]{index=6}
        payload = {
            "prompt": prompt,
            "webhook_url": webhook_url,
            **kwargs,
        }
        return await self.post("/v1/ai/text-to-image/hyperflux", payload)

    async def mystic(self, prompt: str, webhook_url: str, **kwargs) -> Dict[str, Any]:
        # /v1/ai/mystic :contentReference[oaicite:7]{index=7}
        payload = {
            "prompt": prompt,
            "webhook_url": webhook_url,
            **kwargs,
        }
        return await self.post("/v1/ai/mystic", payload)

    # --------- Video (Image->Video) ----------
    async def kling_image_to_video_standard(self, image_base64: str, prompt: str, webhook_url: str, **kwargs) -> Dict[str, Any]:
        # Kling Standard Image-to-Video API :contentReference[oaicite:8]{index=8}
        payload = {
            "image": image_base64,
            "prompt": prompt,
            "webhook_url": webhook_url,
            **kwargs,
        }
        return await self.post("/v1/ai/kling/image-to-video/standard", payload)

    async def kling_image_to_video_pro(self, image_base64: str, prompt: str, webhook_url: str, **kwargs) -> Dict[str, Any]:
        # Kling Pro Image-to-Video API 
        payload = {
            "image": image_base64,
            "prompt": prompt,
            "webhook_url": webhook_url,
            **kwargs,
        }
        return await self.post("/v1/ai/kling/image-to-video/pro", payload)

    # --------- Extras (каркас расширения) ----------
    async def improve_prompt(self, prompt: str) -> Dict[str, Any]:
        # Improve Prompt API exists in Freepik docs :contentReference[oaicite:10]{index=10}
        payload = {"prompt": prompt}
        return await self.post("/v1/ai/improve-prompt", payload)
