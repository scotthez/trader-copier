from tg_signal_trader.suggest import suggest_tps

WOLVES = ("Gold 🏆\nPair: XAUUSD 📊\nSide: Short / Sell Limit\nEntry: 4280 4283\nTP: Open\nSL: 4288\n\n"
          "TP1 4275 50pips ✅\nTP2 4260 100pips ✅\nTP3 4265 150pips ✅")


def test_pips_notes_give_the_suggestion():
    s = suggest_tps("XAUUSD", "SELL", [4275.0, 4260.0, 4265.0], 4280.0, WOLVES)
    assert s["tps"] == [4275.0, 4270.0, 4265.0] and s["changed"] == {"TP2": [4260.0, 4270.0]} and s["basis"] == "pips notes"


def test_without_notes_prefers_a_one_digit_slip_and_the_smallest_change():
    # TP2 4260→4270 (one digit, change 10) beats TP3 4265→4245 (one digit, change 20)
    s = suggest_tps("NAS100", "SELL", [4275.0, 4260.0, 4265.0], 4280.0, "no notes here")
    assert s["tps"] == [4275.0, 4270.0, 4265.0] and s["basis"] == "even spacing"


def test_missing_digit_is_found_from_spacing():
    s = suggest_tps("XAUUSD", "BUY", [4209.0, 4214.0, 4219.0, 2424.0], 4204.0, "")
    assert s["tps"] == [4209.0, 4214.0, 4219.0, 4224.0]


def test_no_suggestion_for_a_valid_ladder_or_hopeless_one():
    assert suggest_tps("XAUUSD", "SELL", [4275.0, 4270.0, 4265.0], 4280.0, "") is None
    assert suggest_tps("XAUUSD", "SELL", [4290.0, 4295.0, 4300.0], 4280.0, "") is None    # every TP on the wrong side
