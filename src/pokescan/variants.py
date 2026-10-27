"""Which printing is the scanned card: normal, holo, reverse holo...?

A fast vision model (Claude Haiku 5.5 through OpenRouter) looks at the crop and
picks one of the variants Scrydex lists for the matched card, or "unsure". The
answer is constrained to that list by a JSON schema, so it can't invent a
printing. About $0.0002 a card. Any failure, timeout or "unsure" leaves the
standard printing in place, so the stream never waits on it.

Key: OPENROUTER_API_KEY in the environment.
"""

import base64
import json
import logging
import os

import aiohttp

from pokescan.scrydex import variant_label

log = logging.getLogger("pokescan.variants")

URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-haiku-5.5"
TIMEOUT = 6.0

# What each kind of printing looks like on camera.
LOOKS = {
    "normal": "no foil anywhere on the card",
    "holo": "the artwork window is foil (rainbow or glittery shine), the text box and frame are plain",
    "reverse": "the card body (frame and text box) is foil, the artwork itself is plain",
    "poke ball": "reverse holo whose foil shows a repeating pattern of Poké Balls",
    "master ball": "reverse holo whose foil shows a repeating pattern of Master Balls",
}


def describe(variant: str) -> str:
    label = variant_label(variant)
    key = label.lower()
    for word, look in (("master ball", LOOKS["master ball"]), ("poke ball", LOOKS["poke ball"]),
                       ("reverse", LOOKS["reverse"]), ("holo", LOOKS["holo"]), ("normal", LOOKS["normal"])):
        if word in key:
            return f"{variant}: {label} ({look})"
    return f"{variant}: {label}"


def prompt(card_name: str, variants: list[str]) -> str:
    options = "\n".join(f"- {describe(v)}" for v in variants)
    return (
        f"This is a webcam photo of the Pokémon card {card_name!r}, held up during a pack opening. "
        f"Which printing is it? It is one of:\n{options}\n"
        "Look for where the foil shine is: on the artwork, on the rest of the card, or nowhere. "
        "Answer 'unsure' if the photo doesn't show it clearly."
    )


def request_body(jpeg: bytes, card_name: str, variants: list[str]) -> dict:
    image = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    return {
        "model": MODEL,
        "max_tokens": 1024,
        "reasoning": {"effort": "low"},
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": image}},
            {"type": "text", "text": prompt(card_name, variants)},
        ]}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "printing", "strict": True,
            "schema": {"type": "object", "additionalProperties": False, "required": ["variant"],
                       "properties": {"variant": {"type": "string", "enum": [*variants, "unsure"]}}},
        }},
    }


class VariantJudge:
    def __init__(self, api_key: str, timeout: float = TIMEOUT):
        self._headers = {"Authorization": f"Bearer {api_key}", "X-Title": "pokescan"}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    @classmethod
    def from_env(cls) -> "VariantJudge | None":
        key = os.environ.get("OPENROUTER_API_KEY")
        return cls(key) if key else None

    async def judge(self, jpeg: bytes, card_name: str, variants: list[str]) -> str | None:
        """One of `variants`, or None when unsure or anything goes wrong."""
        if len(variants) < 2:
            return variants[0] if variants else None
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers, timeout=self._timeout)
        try:
            async with self._session.post(URL, json=request_body(jpeg, card_name, variants)) as response:
                if response.status >= 400:
                    log.warning("variant check: OpenRouter %s: %s", response.status, (await response.text())[:200])
                    return None
                body = await response.json(content_type=None)
            answer = json.loads(body["choices"][0]["message"]["content"])["variant"]
        except (aiohttp.ClientError, TimeoutError, KeyError, IndexError, TypeError, ValueError) as e:
            log.warning("variant check failed: %s", e or type(e).__name__)
            return None
        return answer if answer in variants else None

    async def close(self) -> None:
        if self._session:
            await self._session.close()
