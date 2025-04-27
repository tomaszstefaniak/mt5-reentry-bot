import time

class DummyMT5:
    _orders_called = False
    _pos_called    = False

    # — MT5 trade-type constants (match real MetaTrader5 API) —
    ORDER_TYPE_BUY         = 0    # market buy
    ORDER_TYPE_SELL        = 1    # market sell
    ORDER_TYPE_BUY_LIMIT   = 2
    ORDER_TYPE_SELL_LIMIT  = 3
    TRADE_ACTION_PENDING   = 4

    def initialize(self):
        return True

    def last_error(self):
        return 0

    def orders_get(self):
        """
        First call: return one fake pending BUY_LIMIT.
        Thereafter: none.
        """
        if not DummyMT5._orders_called:
            DummyMT5._orders_called = True
            FakeOrder = type(
                "O", (), {
                    "ticket":         12345,
                    "symbol":         "EURUSD",
                    "type":           DummyMT5.ORDER_TYPE_BUY_LIMIT,
                    "price_open":     1.1000,
                    "volume_initial": 0.1,
                    "sl":             1.0980,
                    "tp":             1.1020,
                }
            )
            return [FakeOrder()]
        return []

    def positions_get(self, ticket=None):
        """
        First call: simulate that the above limit filled (a live position).
        Thereafter: simulate SL/TP hit (no open position).
        """
        if not DummyMT5._pos_called:
            DummyMT5._pos_called = True
            Pos = type("P", (), {
                "ticket":     ticket,
                "symbol":     "EURUSD",
                "type":       DummyMT5.ORDER_TYPE_BUY,      # market buy
                "price_open": 1.1000,
                "volume":     0.1,
                "sl":         1.0980,
                "tp":         1.1020,
            })
            return [Pos()]
        return None

    def symbol_info_tick(self, symbol):
        return type("T", (), {"last": 1.0970})

    def symbol_info(self, symbol):
        return type("SI", (), {"point": 0.0001})

    def order_send(self, request):
        print(f"[STUB] order_send(): {request}")

    def shutdown(self):
        pass

# expose stub as mt5
mt5 = DummyMT5()
