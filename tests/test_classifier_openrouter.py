import openai, pytest
import httpx
from tg_signal_trader.classifier import Classification, EntryExtraction, RunContext
from tg_signal_trader.classifier_openrouter import OpenRouterClassifier, OPENROUTER_BASE_URL

CTX = RunContext(symbol="XAUUSD", side="BUY", entry_type="MARKET", entry_zone=[], sl=4341, open_legs=3, pending_legs=0, sl_current=4341)


class FakeCompletions:
    def __init__(self, outcome): self.outcome = outcome; self.calls = []
    def parse(self, **kw):
        self.calls.append(kw)
        if isinstance(self.outcome, Exception): raise self.outcome
        class Msg: parsed = self.outcome; refusal = None
        class Choice: message = Msg()
        class R: choices = [Choice()]
        return R()


class FakeClient:
    def __init__(self, outcome):
        self.completions = FakeCompletions(outcome); self.opts = None
        class Chat: pass
        self.chat = Chat(); self.chat.completions = self.completions
    def with_options(self, **kw): self.opts = kw; return self


def test_management_happy_path_uses_openai_parse_shape():
    c = FakeClient(Classification(action="break_even", confidence=0.91, reason="im be now"))
    cl = OpenRouterClassifier(model="openai/gpt-5-mini", timeout_sec=8, client=c)
    out = cl.classify_management("im be now", "wolves", CTX, ["RUNNING 40PIPS"])
    assert out.action == "break_even" and out.confidence == 0.91
    kw = c.completions.calls[0]
    assert kw["model"] == "openai/gpt-5-mini" and kw["response_format"] is Classification and c.opts == {"timeout": 8, "max_retries": 1}
    assert kw["messages"][0]["role"] == "system" and "im be now" in kw["messages"][1]["content"] and "XAUUSD" in kw["messages"][1]["content"]
    # Low reasoning effort: this is a structured field-extraction/comparison task, not open-ended
    # judgment, and gpt-5-mini's default reasoning effort was most of the crosscheck's 8-14s latency
    # (a real cost: a slow crosscheck delays a MARKET order into a fast-moving price, see trader.py).
    assert kw["extra_body"] == {"reasoning": {"effort": "low"}}


def test_errors_and_refusals_become_none():
    req = httpx.Request("POST", OPENROUTER_BASE_URL)
    for exc in [openai.APITimeoutError(request=req), openai.APIConnectionError(request=req),
                openai.RateLimitError("rl", response=httpx.Response(429, request=req), body=None),
                openai.APIStatusError("boom", response=httpx.Response(500, request=req), body=None), ValueError("schema")]:
        out = OpenRouterClassifier(model="m", timeout_sec=1, client=FakeClient(exc)).classify_management("x", "wolves", None, [])
        assert out.action == "none" and out.confidence == 0 and out.reason.startswith("error:")
    out = OpenRouterClassifier(model="m", timeout_sec=1, client=FakeClient(None)).classify_management("x", "wolves", None, [])
    assert out.action == "none" and "no parsed" in out.reason


def test_entry_extraction():
    ex = EntryExtraction(symbol="XAUUSD", side="BUY", entry_type="MARKET", entry_zone=[], sl=4341, tps=[4353, 4357, 4362, None], confidence=0.9)
    assert OpenRouterClassifier(model="m", timeout_sec=1, client=FakeClient(ex)).extract_entry("x", "wolves") == ex
    assert OpenRouterClassifier(model="m", timeout_sec=1, client=FakeClient(None)).extract_entry("x", "wolves") is None


def test_real_client_points_at_openrouter():
    cl = OpenRouterClassifier(model="m", timeout_sec=1, api_key="sk-test")
    assert str(cl.client.base_url).rstrip("/") == OPENROUTER_BASE_URL
