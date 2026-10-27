"""A thin async client for the parts of obs-websocket v5 that pokescan uses."""

import base64

import simpleobsws

from pokescan.config import ObsSettings


class ObsError(Exception):
    pass


class Obs:
    def __init__(self, settings: ObsSettings, timeout: float = 8.0):
        self.settings = settings
        self.timeout = timeout
        # No event subscriptions: we only make requests, and events add traffic.
        self._ws = simpleobsws.WebSocketClient(
            url=settings.url,
            password=settings.password,
            identification_parameters=simpleobsws.IdentificationParameters(eventSubscriptions=0),
        )

    async def __aenter__(self) -> "Obs":
        try:
            await self._ws.connect()
        except OSError as e:
            raise ObsError(
                f"Can't reach OBS at {self.settings.url}. Is OBS open, with "
                "Tools → WebSocket Server Settings → Enable WebSocket server ticked?"
            ) from e
        if not await self._ws.wait_until_identified(timeout=self.timeout):
            await self._ws.disconnect()
            raise ObsError(
                f"OBS at {self.settings.url} didn't accept the connection. "
                f"Check the WebSocket password (read from {self.settings.source})."
            )
        return self

    async def __aexit__(self, *exc) -> None:
        await self._ws.disconnect()

    async def request(self, request_type: str, **data) -> dict:
        try:
            response = await self._ws.call(simpleobsws.Request(request_type, data or None), timeout=self.timeout)
        except simpleobsws.MessageTimeout as e:
            raise ObsError(f"{request_type} timed out after {self.timeout}s") from e
        if not response.ok():
            status = response.requestStatus
            raise ObsError(f"{request_type} failed ({status.code}): {status.comment}")
        return response.responseData or {}

    async def inputs(self) -> list[dict]:
        return (await self.request("GetInputList"))["inputs"]

    async def screenshot(self, source: str, width: int | None = None, fmt: str = "jpg", quality: int = 85) -> bytes:
        """One frame of `source`, encoded by OBS. `width` keeps the aspect ratio."""
        data = {"sourceName": source, "imageFormat": fmt, "imageCompressionQuality": quality}
        if width is not None:
            data["imageWidth"] = width
        image_data = (await self.request("GetSourceScreenshot", **data))["imageData"]
        # A data URI: "data:image/jpg;base64,...."
        return base64.b64decode(image_data.split(",", 1)[1])

    async def stats(self) -> dict:
        return await self.request("GetStats")
