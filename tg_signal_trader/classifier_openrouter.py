"""OpenRouter-backed classifier (OpenAI-compatible API, structured outputs). Same contract as ClaudeClassifier."""
from __future__ import annotations
import openai
from .classifier import Classification, EntryExtraction, RunContext, MANAGEMENT_SYSTEM, ENTRY_SYSTEM

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_ERRORS = (openai.OpenAIError, ValueError)


class OpenRouterClassifier:
    def __init__(self, model: str, timeout_sec: float, api_key: str | None = None, client=None):
        self.model, self.timeout = model, timeout_sec
        self.client = client or openai.OpenAI(api_key=api_key, base_url=OPENROUTER_BASE_URL)

    def _parse(self, system: str, user: str, schema):
        completion = self.client.with_options(timeout=self.timeout, max_retries=1).chat.completions.parse(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format=schema)
        return completion.choices[0].message.parsed

    def classify_management(self, text: str, provider: str, ctx: RunContext | None, recent: list[str]) -> Classification:
        context = ctx.model_dump_json() if ctx else "no open trade"
        user = (f"Provider: {provider}\nOpen trade context: {context}\nRecent provider messages (oldest first):\n"
                + "\n".join(f"- {r}" for r in recent) + f"\n\nMessage to classify:\n{text}")
        try:
            parsed = self._parse(MANAGEMENT_SYSTEM, user, Classification)
        except _ERRORS as e:
            return Classification(action="none", confidence=0.0, reason=f"error: {type(e).__name__}")
        if parsed is None:
            return Classification(action="none", confidence=0.0, reason="error: no parsed output (refusal or empty)")
        return parsed

    def extract_entry(self, text: str, provider: str) -> EntryExtraction | None:
        try:
            return self._parse(ENTRY_SYSTEM, f"Provider: {provider}\nMessage:\n{text}", EntryExtraction)
        except _ERRORS:
            return None
