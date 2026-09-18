import anthropic, pytest
import httpx2 as httpx   # anthropic>=1.0 depends on httpx2
from tg_signal_trader.classifier import ClaudeClassifier, MockClassifier, Classification, EntryExtraction, RunContext, NONE

CTX = RunContext(symbol="XAUUSD", side="BUY", entry_type="MARKET", entry_zone=[], sl=4341, open_legs=3, pending_legs=0, sl_current=4341)


class FakeMessages:
    def __init__(self, outcome): self.outcome = outcome; self.calls = []
    def parse(self, **kw):
        self.calls.append(kw)
        if isinstance(self.outcome, Exception): raise self.outcome
        class R: parsed_output = self.outcome
        return R()


class FakeClient:
    def __init__(self, outcome): self.messages = FakeMessages(outcome); self.opts = None
    def with_options(self, **kw): self.opts = kw; return self


def test_management_happy_path_passes_context_and_uses_parse():
    c = FakeClient(Classification(action="move_sl", price=4412, confidence=0.93, reason="explicit SL price"))
    cl = ClaudeClassifier(model="claude-opus-5", timeout_sec=8, client=c)
    out = cl.classify_management("Move SL 4412", "wolves", CTX, ["RUNNING 40PIPS"])
    assert out.action == "move_sl" and out.price == 4412 and out.confidence == 0.93
    kw = c.messages.calls[0]
    assert kw["model"] == "claude-opus-5" and kw["output_format"] is Classification and c.opts == {"timeout": 8, "max_retries": 1}
    user = kw["messages"][0]["content"]
    assert "Move SL 4412" in user and "XAUUSD" in user and "RUNNING 40PIPS" in user and "wolves" in user


def test_management_errors_become_none():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    for exc in [anthropic.APITimeoutError(request=req), anthropic.APIConnectionError(request=req),
                anthropic.RateLimitError("rl", response=httpx.Response(429, request=req), body=None),
                anthropic.APIStatusError("boom", response=httpx.Response(500, request=req), body=None), ValueError("schema")]:
        out = ClaudeClassifier(model="m", timeout_sec=1, client=FakeClient(exc)).classify_management("x", "wolves", None, [])
        assert out.action == "none" and out.confidence == 0 and out.reason.startswith("error:")


def test_entry_extraction_and_error():
    ex = EntryExtraction(symbol="XAUUSD", side="BUY", entry_type="MARKET", entry_zone=[], sl=4341, tps=[4353, 4357, 4362, None], confidence=0.9)
    assert ClaudeClassifier(model="m", timeout_sec=1, client=FakeClient(ex)).extract_entry("buy gold 4347 sl 4341 tp 4353 4357 4362", "wolves") == ex
    req = httpx.Request("POST", "https://x")
    assert ClaudeClassifier(model="m", timeout_sec=1, client=FakeClient(anthropic.APITimeoutError(request=req))).extract_entry("x", "wolves") is None


def test_mock_classifier():
    m = MockClassifier(management={"Delete this": Classification(action="close_all", confidence=0.95)})
    assert m.classify_management("Delete this", "wolves", None, []).action == "close_all"
    assert m.classify_management("hello", "wolves", None, []) == NONE
    assert m.extract_entry("x", "wolves") is None


def test_schema_rejects_unknown_action():
    with pytest.raises(ValueError):
        Classification(action="secure_half", confidence=1.0)


def test_reply_text_is_included_in_prompt():
    c = FakeClient(Classification(action="cancel_pending", confidence=0.9))
    ClaudeClassifier(model="m", timeout_sec=1, client=c).classify_management("Delete this", "wolves", None, [], reply_text="SELL Limit XAUUSD @4290 4295")
    user = c.messages.calls[0]["messages"][0]["content"]
    assert "REPLY quoting" in user and "SELL Limit XAUUSD @4290 4295" in user
    c2 = FakeClient(Classification(action="none", confidence=0.9))
    ClaudeClassifier(model="m", timeout_sec=1, client=c2).classify_management("x", "wolves", None, [])
    assert "not a reply" in c2.messages.calls[0]["messages"][0]["content"]
