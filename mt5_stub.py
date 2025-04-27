# mt5_stub.py

import time

class DummyMT5:
    _called = False

    # MT5 constants (stubbed)
    ORDER_TYPE_BUY_LIMIT  = 0
    ORDER_TYPE_SELL_LIMIT = 1
    TRADE_ACTION_PENDING  = 1
    ORDER_STATE_FILLED    = 4   # match real MT5 constant

    def initialize(self):
        return True

    def last_error(self):
        return 0

    def orders_get(self):
        # On first call, return one pending order object
        if not DummyMT5._called:
            DummyMT5._called = True
            FakeOrder = type(
                "O", (), {
                    "ticket": 12345,
                    "symbol": "EURUSD",
                    "type": DummyMT5.ORDER_TYPE_BUY_LIMIT,
                    "price_open": 1.1000,
                    "volume_initial": 0.1,
                    "sl": 1.0980,
                    "tp": 1.1020,
                    "state": None,   # pending
                }
            )
            return [FakeOrder()]
        # On subsequent calls, return it as "filled"
        FilledOrder = type(
            "O", (), {
                "ticket": 12345,
                "symbol": "EURUSD",
                "type": DummyMT5.ORDER_TYPE_BUY_LIMIT,
                "price_open": 1.1000,
                "volume_initial": 0.1,
                "sl": 1.0980,
                "tp": 1.1020,
                "state": DummyMT5.ORDER_STATE_FILLED,
            }
        )
        return [FilledOrder()]

    def positions_get(self, ticket=None):
        # After "fill", simulate immediate SL hit by returning None
        return None

    def symbol_info_tick(self, symbol):
        return type("T", (), {"last": 1.0970})

    def symbol_info(self, symbol):
        return type("SI", (), {"point": 0.0001})

    def order_send(self, request):
        print(f"[STUB] order_send(): {request}")

    def shutdown(self):
        pass

# Expose the stub as 'mt5'
mt5 = DummyMT5()
