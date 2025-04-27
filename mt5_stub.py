import time

class DummyMT5:
    _called = False

    # MT5 constants (stub)
    ORDER_TYPE_BUY_LIMIT = 0
    ORDER_TYPE_SELL_LIMIT = 1
    TRADE_ACTION_PENDING = 1
    ORDER_STATE_FILLED = 4    # mirror real MT5 constant

    def initialize(self):
        return True

    def last_error(self):
        return 0

    def orders_get(self):
        # Return one fake pending order once
        if not DummyMT5._called:
            DummyMT5._called = True
            FakeOrder = type(
                "O",
                (),
                {
                    "ticket": 12345,
                    "symbol": "EURUSD",
                    "type": DummyMT5.ORDER_TYPE_BUY_LIMIT,
                    "price_open": 1.1000,
                    "volume_initial": 0.1,
                    "sl": 1.0980,
                    "tp": 1.1020,
                    "state": None,   # pending state
                },
            )
            return [FakeOrder()]
        return []

    def order_get(self, ticket):
        # Simulate fill after a short delay
        time.sleep(0.5)
        # Return an object with .state == FILLED
        return type("O", (), {"ticket": ticket, "state": DummyMT5.ORDER_STATE_FILLED})

    def positions_get(self, ticket=None):
        # Always simulate SL hit by returning None
        return None

    def symbol_info_tick(self, symbol):
        return type("T", (), {"last": 1.0970})

    def symbol_info(self, symbol):
        return type("SI", (), {"point": 0.0001})

    def order_send(self, request):
        print(f"[STUB] order_send(): {request}")

    def shutdown(self):
        pass

mt5 = DummyMT5()
