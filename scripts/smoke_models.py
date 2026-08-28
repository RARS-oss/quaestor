"""Quick import/construct smoke for quaestor.models (no network)."""
from quaestor.models import Leg, PositionIntent, Side, Structure, TradeIntent, new_client_order_id

leg = Leg("SPY260904C00650000", Side.BUY, 1, PositionIntent.BUY_TO_OPEN)
i = TradeIntent(
    underlying="SPY", structure=Structure.VERTICAL_DEBIT, legs=[leg],
    qty=1, limit_price=1.25, thesis="smoke", max_loss_usd=125.0,
)
assert not i.is_credit and not i.is_multileg
d = i.to_dict()
assert d["legs"][0]["side"] == "buy"
print("models OK", i.intent_id, new_client_order_id(i.intent_id, 1, "deadbeefcafe"))
