#!/usr/bin/env python
#  -*- coding: utf-8 -*-
__author__ = 'mayanqiong'

import math
from datetime import datetime
from typing import Callable

from tqsdk.datetime import _is_in_trading_time
from tqsdk.diff import _simple_merge_diff
from tqsdk.sim.utils import _get_price_range, _get_option_margin, _get_premium, _get_close_profit, _get_commission, \
    _get_future_margin


class SimTrade(object):
    """
    天勤模拟交易账户，处理 orderbook 和撮合交易

    计算账户资金、持仓信息

    本模块为 TqSim 交易部分的子模块，纯同步计算，不涉及连接行情的动态信息，所以对于接口的调用有一些需要注意的要求

    提供的接口:

    + init_snapshot: 返回初始的账户截面信息

    + insert_order: 处理下单请求，调用 TqSimAccount.insert_order 之前应该调用过 update_quote，保证收到过合约的行情；期权还应该确保收到了标的的行情

    + cancel_order：处理撤单请求

    + update_quote：处理行情更新

        返回值 diffs

            diffs 是 list 类型，每个元素都是符合 diff 协议中 trade 交易部分的数据包，且返回的都是完整字段的对象，比如：order成交时，返回的是order完整对象而不是有变化的字段

    + settle：处理结算请求

        返回值 diffs, trade_log

            diffs 同上，trade_log 是结算前的账户日志信息

    """

    def __init__(self, account_key: str, init_balance: float = 10000000.0, get_trade_timestamp: Callable = None,
                 is_in_trading_time: Callable = None) -> None:
        self._account_key = account_key
        self._quotes = {}  # 会记录所有的发来的行情

        self._account = {
            "currency": "CNY",
            "pre_balance": init_balance,
            "static_balance": init_balance,
            "balance": init_balance,
            "available": init_balance,
            "float_profit": 0.0,
            "position_profit": 0.0,  # 期权没有持仓盈亏
            "close_profit": 0.0,
            "frozen_margin": 0.0,
            "margin": 0.0,
            "frozen_commission": 0.0,
            "commission": 0.0,
            "frozen_premium": 0.0,
            "premium": 0.0,
            "deposit": 0.0,
            "withdraw": 0.0,
            "risk_ratio": 0.0,
            "market_value": 0.0,
            "ctp_balance": float("nan"),
            "ctp_available": float("nan")
        }
        self._positions = {}  # {symbol: position, ...}
        self._orders = {}  # {symbol: {order_id: order}, ...}
        self._trades = []  # list 类型，与重构之前代码保持一致，list 可以保留 trade 生产的顺序信息

        self._diffs = []
        self._orders_events = []  # 按照顺序记录 order 的更新，返回给调用方

        self._max_datetime = ""  # 所有 quotes 的最大行情更新时间
        # 本模块在计算成交时间、判断是否在交易时间段内，默认使用所有 quotes 的最大行情更新时间当作当前时间，并且没有模拟到交易时的时间差
        # 若外部调用模块需要更精确时间，则由外部模块提供函数支持
        self._get_trade_timestamp = get_trade_timestamp if get_trade_timestamp else self._default_get_trade_timestamp
        self._is_in_trading_time = is_in_trading_time if is_in_trading_time else self._default_is_in_trading_time

    def insert_order(self, symbol, pack):
        quote, underlying_quote = self._get_quotes_by_symbol(symbol)

        order = self._pre_insert_order(pack)
        orders = self._orders.setdefault(symbol, {})
        orders[order["order_id"]] = order  # order 存入全局
        self._orders_events.append(order.copy())

        self._insert_order(order, symbol, quote, underlying_quote)
        if order["status"] == "ALIVE":
            self._match_order(order, symbol, quote, underlying_quote)
        if order["status"] == "FINISHED":
            self._orders_events.append(order)
            del self._orders[symbol][order["order_id"]]  # 删除 order
        return self._return_results()

    def cancel_order(self, symbol, pack):
        order = self._orders.get(symbol, {}).get(pack["order_id"], {})
        if order.get("status") == "ALIVE":
            order["last_msg"] = "已撤单"
            order["status"] = "FINISHED"
            self._on_order_failed(symbol, order)
            self._orders_events.append(order)
            del self._orders[symbol][order["order_id"]]  # 删除 order
        return self._return_results()

    def update_quotes(self, symbol, pack):
        for q in pack.get("quotes", {}).values():
            self._max_datetime = max(q.get("datetime", ""), self._max_datetime)
        _simple_merge_diff(self._quotes, pack.get("quotes", {}), reduce_diff=False)

        quote, underlying_quote = self._get_quotes_by_symbol(symbol)
        # 某些非交易时间段，ticks 回测是 quote 的最新价有可能是 nan，无效的行情直接跳过
        if math.isnan(quote["last_price"]):
            return [], []

        # 撮合委托单
        orders = self._orders.get(symbol, {})
        for order_id in list(orders.keys()):  # match_order 过程中可能会删除 orders 下对象
            self._match_order(orders[order_id], symbol, quote, underlying_quote)
            if orders[order_id]["status"] == "FINISHED":
                self._orders_events.append(orders[order_id])
                del self._orders[symbol][order_id]

        # 调整持仓保证金和盈亏
        position = self._ensure_position(symbol)
        underlying_last_price = underlying_quote["last_price"] if underlying_quote else float('nan')
        future_margin = _get_future_margin(quote)
        if position["volume_long"] > 0 or position["volume_short"] > 0:
            if position["last_price"] != quote["last_price"] \
                    or (math.isnan(future_margin) or future_margin != position["future_margin"])\
                    or (underlying_quote and (math.isnan(underlying_last_price) or underlying_last_price != position["underlying_last_price"])):
                self._adjust_position_account(symbol, quote, underlying_quote,
                                              pre_last_price=position["last_price"],
                                              last_price=quote["last_price"],
                                              pre_underlying_last_price=position["underlying_last_price"],
                                              underlying_last_price=underlying_last_price)
                position["future_margin"] = future_margin
                position["last_price"] = quote["last_price"]
                position["underlying_last_price"] = underlying_last_price
        else:
            # 修改辅助变量
            position["future_margin"] = future_margin
            position["last_price"] = quote["last_price"]
            position["underlying_last_price"] = underlying_last_price
        self._send_position(position)  # 一定要返回 position，下游会用到 future_margin 字段判断修改保证金是否成功
        self._send_account()
        return self._return_results()

    def settle(self):
        trade_log = {
            "trades": self._trades,
            "account": self._account.copy(),
            "positions": {k: v.copy() for k, v in self._positions.items()}
        }

        # 为下一交易日调整账户
        self._trades = []

        for symbol in self._orders:
            for order in self._orders[symbol].values():
                order["frozen_margin"] = 0.0
                order["frozen_premium"] = 0.0
                order["last_msg"] = "交易日结束，自动撤销当日有效的委托单（GFD）"
                order["status"] = "FINISHED"
                self._orders_events.append(order)
                self._send_order(order)
            self._orders[symbol] = {}

        # account 原始字段
        self._account["pre_balance"] = self._account["balance"] - self._account["market_value"]
        self._account["close_profit"] = 0.0
        self._account["commission"] = 0.0
        self._account["premium"] = 0.0
        self._account["frozen_margin"] = 0.0
        self._account["frozen_premium"] = 0.0
        # account 计算字段
        self._account["static_balance"] = self._account["pre_balance"]
        self._account["position_profit"] = 0.0
        self._account["risk_ratio"] = self._account["margin"] / self._account["balance"]
        self._account["available"] = self._account["static_balance"] - self._account["margin"]
        # 根据公式 账户权益 不需要计算 self._account["balance"] = static_balance + market_value
        self._send_account()

        # 对于持仓的结算放在这里，没有放在 quote_handler 里的原因：
        # 1. 异步发送的话，会造成如果此时 sim 未收到 pending_peek, 就没法把结算的账户信息发送出去，此时用户代码中 api.get_postion 得到的持仓和 sim 里面的持仓是不一致的
        # set_target_pos 下单时就会产生错单。而且结算时一定是已经收到过行情的数据包，在同步代码的最后一步，会发送出去这个行情包 peeding_peek，
        # quote_handler 处理 settle 的时候, 所以在结算的时候 pending_peek 一定是 False, 要 api 处理过之后，才会收到 peek_message
        # 2. 同步发送的话，就可以和产生切换交易日的数据包同时发送出去
        # 对 order 的处理发生在下一次回复 peek_message
        for position in self._positions.values():
            # position 原始字段
            position["volume_long_frozen_today"] = 0
            position["volume_long_frozen_his"] = 0
            position["volume_short_frozen_today"] = 0
            position["volume_short_frozen_his"] = 0
            position["volume_long_today"] = 0
            position["volume_long_his"] = position["volume_long"]
            position["volume_short_today"] = 0
            position["volume_short_his"] = position["volume_short"]
            # position 计算字段
            position["pos_long_his"] = position["volume_long_his"]
            position["pos_long_today"] = 0
            position["pos_short_his"] = position["volume_short_his"]
            position["pos_short_today"] = 0
            position["volume_long_frozen"] = 0
            position["volume_short_frozen"] = 0
            position["position_price_long"] = position["last_price"]
            position["position_price_short"] = position["last_price"]
            quote, _ = self._get_quotes_by_symbol(f"{position['exchange_id']}.{position['instrument_id']}")
            position["position_cost_long"] = position["last_price"] * position["volume_long"] * quote["volume_multiple"]  # position 原始字段
            position["position_cost_short"] = position["last_price"] * position["volume_short"] * quote["volume_multiple"]  # position 原始字段
            position["position_profit_long"] = 0
            position["position_profit_short"] = 0
            position["position_profit"] = 0
            self._send_position(position)
        diffs, orders_events = self._return_results()
        return diffs, orders_events, trade_log

    def init_snapshot(self):
        """返回初始账户截面信息"""
        return {
            "trade": {
                self._account_key: {
                    "accounts": {"CNY": self._account.copy()},
                    "positions": {},
                    "orders": {},
                    "trades": {}
                }
            }
        }

    def _return_results(self):
        """
        返回两项内容：diffs: list, orders_events: list
        diffs 是截面的变更
        orders_events 是委托单变化
        """
        diffs = self._diffs
        self._diffs = []
        orders_events = self._orders_events
        self._orders_events = []
        return diffs, orders_events

    def _ensure_position(self, symbol):
        position = self._positions.setdefault(symbol,  {
            "exchange_id": symbol.split(".", maxsplit=1)[0],
            "instrument_id": symbol.split(".", maxsplit=1)[1],
            "pos_long_his": 0,
            "pos_long_today": 0,
            "pos_short_his": 0,
            "pos_short_today": 0,
            "volume_long_today": 0,
            "volume_long_his": 0,
            "volume_long": 0,
            "volume_long_frozen_today": 0,
            "volume_long_frozen_his": 0,
            "volume_long_frozen": 0,
            "volume_short_today": 0,
            "volume_short_his": 0,
            "volume_short": 0,
            "volume_short_frozen_today": 0,
            "volume_short_frozen_his": 0,
            "volume_short_frozen": 0,
            "open_price_long": float("nan"),
            "open_price_short": float("nan"),
            "open_cost_long": 0.0,
            "open_cost_short": 0.0,
            "position_price_long": float("nan"),
            "position_price_short": float("nan"),
            "position_cost_long": 0.0,
            "position_cost_short": 0.0,
            "float_profit_long": 0.0,
            "float_profit_short": 0.0,
            "float_profit": 0.0,
            "position_profit_long": 0.0,
            "position_profit_short": 0.0,
            "position_profit": 0.0,
            "margin_long": 0.0,
            "margin_short": 0.0,
            "margin": 0.0,
            "last_price": float('nan'),
            "underlying_last_price": float('nan'),
            "market_value_long": 0.0,  # 权利方市值(始终 >= 0)
            "market_value_short": 0.0,  # 义务方市值(始终 <= 0)
            "market_value": 0.0,
        })
        if math.isnan(position["last_price"]):
            # 该持仓第一次添加，添加辅助计算字段，last_price underlying_last_price
            quote, underlying_quote = self._get_quotes_by_symbol(symbol)
            position["future_margin"] = _get_future_margin(quote)
            position["last_price"] = quote["last_price"]
            position["underlying_last_price"] = underlying_quote["last_price"] if underlying_quote else float("nan")
        return position

    def _get_quotes_by_symbol(self, symbol):
        """返回指定合约及标的合约，在本模块执行过程中，应该保证一定有合约行情"""
        quote = self._quotes.get(symbol)
        assert quote and quote.get("datetime"), "未收到指定合约行情"
        underlying_quote = None
        if quote["ins_class"].endswith("OPTION"):
            underlying_quote = self._quotes.get(quote["underlying_symbol"])
            assert underlying_quote and underlying_quote.get("datetime"), "未收到指定合约的标的行情"
        return quote, underlying_quote

    def _pre_insert_order(self, pack):
        """order 对象预处理"""
        order = pack.copy()
        order["exchange_order_id"] = order["order_id"]
        order["volume_orign"] = order["volume"]
        order["volume_left"] = order["volume"]
        order["frozen_margin"] = 0.0
        order["frozen_premium"] = 0.0
        order["last_msg"] = "报单成功"
        order["status"] = "ALIVE"
        order["insert_date_time"] = self._get_trade_timestamp()
        del order["aid"]
        del order["volume"]
        self._send_order(order)
        return order

    def _insert_order(self, order, symbol, quote, underlying_quote=None):
        """判断 order 是否可以记录在 orderbook"""
        if ("commission" not in quote or "margin" not in quote) and not quote["ins_class"].endswith("OPTION"):
            order["last_msg"] = "不支持的合约类型，TqSim 目前不支持组合，股票，etf期权模拟交易"
            order["status"] = "FINISHED"

        if order["status"] == "ALIVE" and not self._is_in_trading_time(quote):
            order["last_msg"] = "下单失败, 不在可交易时间段内"
            order["status"] = "FINISHED"

        position = self._ensure_position(symbol)
        if order["status"] == "ALIVE" and order["offset"].startswith('CLOSE'):
            if order["exchange_id"] in ["SHFE", "INE"]:
                if order["offset"] == "CLOSETODAY":
                    if order["direction"] == "BUY" and position["volume_short_today"] - position["volume_long_frozen_today"] < order["volume_orign"]:
                        order["last_msg"] = "平今仓手数不足"
                    elif order["direction"] == "SELL" and position["volume_long_today"] - position["volume_long_frozen_today"] < order["volume_orign"]:
                        order["last_msg"] = "平今仓手数不足"
                if order["offset"] == "CLOSE":
                    if order["direction"] == "BUY" and position["volume_short_his"] - position["volume_short_frozen_his"] < order["volume_orign"]:
                        order["last_msg"] = "平昨仓手数不足"
                    elif order["direction"] == "SELL" and position["volume_long_his"] - position["volume_long_frozen_his"] < order["volume_orign"]:
                        order["last_msg"] = "平昨仓手数不足"
            else:
                if order["direction"] == "BUY" and position["volume_short"] - position["volume_short_frozen"] < order["volume_orign"]:
                    order["last_msg"] = "平仓手数不足"
                elif order["direction"] == "SELL" and position["volume_long"] - position["volume_long_frozen"] < order["volume_orign"]:
                    order["last_msg"] = "平仓手数不足"
            if order["last_msg"].endswith("手数不足"):
                order["status"] = "FINISHED"

        if order["status"] == "ALIVE" and order["offset"] == "OPEN":
            # 计算冻结保证金，冻结权利金
            if quote["ins_class"].endswith("OPTION"):
                if order["direction"] == "SELL":  # 期权的SELL义务仓，开仓需要冻结保证金
                    order["frozen_margin"] = order["volume_orign"] * _get_option_margin(quote, quote["last_price"], underlying_quote["last_price"])
                else:  # 期权的BUY权利仓（市价单使用 last_price 计算需要冻结的权利金）
                    price = quote["last_price"] if order["price_type"] == "ANY" else order["limit_price"]
                    order["frozen_premium"] = order["volume_orign"] * quote["volume_multiple"] * price
            else:
                order["frozen_margin"] = order["volume_orign"] * _get_future_margin(quote)
            if order["frozen_margin"] + order["frozen_premium"] > self._account["available"]:
                order["frozen_margin"] = 0.0
                order["frozen_premium"] = 0.0
                order["last_msg"] = '开仓资金不足'
                order["status"] = "FINISHED"

        if order["status"] == "FINISHED":
            self._send_order(order)

        if order["status"] == "ALIVE" and order["offset"] == "OPEN":
            # 修改 account 计算字段
            self._adjust_account_by_order(frozen_margin=order["frozen_margin"], frozen_premium=order["frozen_premium"])
            self._send_account()

        if order["status"] == 'ALIVE' and order["offset"].startswith('CLOSE'):
            # 修改 position 原始字段
            if order["exchange_id"] in ["SHFE", "INE"]:
                if order["direction"] == "BUY":
                    position[f"volume_short_frozen_{'today' if order['offset'] == 'CLOSETODAY' else 'his'}"] += order["volume_orign"]
                else:
                    position[f"volume_long_frozen_{'today' if order['offset'] == 'CLOSETODAY' else 'his'}"] += order["volume_orign"]
            elif order["direction"] == "BUY":
                volume_short_his_available = position["volume_short_his"] - position["volume_short_frozen_his"]
                if volume_short_his_available < order["volume_orign"]:
                    position["volume_short_frozen_his"] += volume_short_his_available
                    position["volume_short_frozen_today"] += order["volume_orign"] - volume_short_his_available
                else:
                    position["volume_short_frozen_his"] += order["volume_orign"]
            else:
                volume_long_his_available = position["volume_long_his"] - position["volume_long_frozen_his"]
                if volume_long_his_available < order["volume_orign"]:
                    position["volume_long_frozen_his"] += volume_long_his_available
                    position["volume_long_frozen_today"] += order["volume_orign"] - volume_long_his_available
                else:
                    position["volume_long_frozen_his"] += order["volume_orign"]
            # 修改 position 计算字段
            self._adjust_position_volume_frozen(position)
            self._send_position(position)

    def _match_order(self, order, symbol, quote, underlying_quote=None):
        assert order["status"] == "ALIVE"
        ask_price, bid_price = _get_price_range(quote)
        # order 预期成交价格
        if order["price_type"] in ["ANY", "BEST", "FIVELEVEL"]:
            price = ask_price if order["direction"] == "BUY" else bid_price
        else:
            price = order["limit_price"]

        if order["price_type"] == "ANY" and math.isnan(price):
            order["last_msg"] = "市价指令剩余撤销"
            order["status"] = "FINISHED"

        if order["time_condition"] == "IOC":  # IOC 立即成交，限价下单且不能成交的价格，直接撤单
            if order["direction"] == "BUY" and price < ask_price or order["direction"] == "SELL" and price > bid_price:
                order["last_msg"] = "已撤单报单已提交"
                order["status"] = "FINISHED"

        if order["status"] == "FINISHED":
            self._on_order_failed(symbol, order)
        elif order["direction"] == "BUY" and price >= ask_price or order["direction"] == "SELL" and price <= bid_price:
            trade_id = order["order_id"] + "|" + str(order["volume_left"])
            trade = {
                "user_id": order["user_id"],
                "order_id": order["order_id"],
                "trade_id": trade_id,
                "exchange_trade_id": order["order_id"] + "|" + str(order["volume_left"]),
                "exchange_id": order["exchange_id"],
                "instrument_id": order["instrument_id"],
                "direction": order["direction"],
                "offset": order["offset"],
                "price": price,
                "volume": order["volume_left"],
                "trade_date_time": self._get_trade_timestamp(),  # todo: 可能导致测试结果不确定
                "commission": order["volume_left"] * _get_commission(quote)
            }
            self._trades.append(trade)
            self._send_trade(trade)
            self._on_order_traded(order, trade, quote, underlying_quote)

    def _on_order_traded(self, order, trade, quote, underlying_quote):
        symbol = order["exchange_id"] + "." + order["instrument_id"]
        origin_frozen_margin = order["frozen_margin"]
        origin_frozen_premium = order["frozen_premium"]
        order["frozen_margin"] = 0.0
        order["frozen_premium"] = 0.0
        order["volume_left"] = 0
        order["last_msg"] = "全部成交"
        order["status"] = "FINISHED"
        self._send_order(order)

        position = self._ensure_position(symbol)
        if order["offset"] == 'OPEN':
            if order["direction"] == "BUY":
                # 修改 position 原始字段
                position["volume_long_today"] += order["volume_orign"]
                position["open_cost_long"] += trade["price"] * order["volume_orign"] * quote["volume_multiple"]  # 多头开仓成本
                position["position_cost_long"] += trade["price"] * order["volume_orign"] * quote["volume_multiple"]  # 多头持仓成本
            else:
                # 修改 position 原始字段
                position["volume_short_today"] += order["volume_orign"]
                position["open_cost_short"] += trade["price"] * order["volume_orign"] * quote["volume_multiple"]  # 空头开仓成本
                position["position_cost_short"] += trade["price"] * order["volume_orign"] * quote["volume_multiple"]  # 空头持仓成本

            # 由 order 变化，account 需要更新的计算字段
            self._adjust_account_by_order(frozen_margin=-origin_frozen_margin, frozen_premium=-origin_frozen_premium)

            # 由 trade 引起的 account 原始字段变化，account 需要更新的计算字段
            premium = _get_premium(trade, quote)
            self._adjust_account_by_trade(commission=trade["commission"], premium=premium)

            # 由 position 字段变化，同时 account 需要更新的计算字段
            buy_open = order["volume_orign"] if order["direction"] == "BUY" else 0
            sell_open = 0 if order["direction"] == "BUY" else order["volume_orign"]
            self._adjust_position_account(symbol, quote, underlying_quote,
                                          pre_last_price=trade["price"],
                                          last_price=position["last_price"],
                                          pre_underlying_last_price=underlying_quote["last_price"] if underlying_quote else float('nan'),
                                          underlying_last_price=position["underlying_last_price"],
                                          buy_open=buy_open, sell_open=sell_open)

        else:  # order["offset"].startswith('CLOSE')
            # 修改 position 原始字段
            if order["exchange_id"] in ["SHFE", "INE"]:
                if order["offset"] == "CLOSETODAY":
                    if order["direction"] == "BUY":
                        position["volume_short_frozen_today"] -= order["volume_orign"]
                        position["volume_short_today"] -= order["volume_orign"]
                    elif order["direction"] == "SELL":
                        position["volume_long_frozen_today"] -= order["volume_orign"]
                        position["volume_long_today"] -= order["volume_orign"]
                if order["offset"] == "CLOSE":
                    if order["direction"] == "BUY":
                        position["volume_short_frozen_his"] -= order["volume_orign"]
                        position["volume_short_his"] -= order["volume_orign"]
                    elif order["direction"] == "SELL":
                        position["volume_long_frozen_his"] -= order["volume_orign"]
                        position["volume_long_his"] -= order["volume_orign"]
            elif order["direction"] == "BUY":
                if position["volume_short_frozen_his"] >= order["volume_orign"]:
                    position["volume_short_frozen_his"] -= order["volume_orign"]
                    position["volume_short_his"] -= order["volume_orign"]
                else:
                    position["volume_short_frozen_today"] -= order["volume_orign"] - position["volume_short_frozen_his"]
                    position["volume_short_today"] -= order["volume_orign"] - position["volume_short_frozen_his"]
                    position["volume_short_his"] -= position["volume_short_frozen_his"]
                    position["volume_short_frozen_his"] = 0
            else:
                if position["volume_long_frozen_his"] >= order["volume_orign"]:
                    position["volume_long_frozen_his"] -= order["volume_orign"]
                    position["volume_long_his"] -= order["volume_orign"]
                else:
                    position["volume_long_frozen_today"] -= order["volume_orign"] - position["volume_long_frozen_his"]
                    position["volume_long_today"] -= order["volume_orign"] - position["volume_long_frozen_his"]
                    position["volume_long_his"] -= position["volume_long_frozen_his"]
                    position["volume_long_frozen_his"] = 0

            # 修改 position 原始字段
            if order["direction"] == "SELL":
                position["open_cost_long"] -= position["open_price_long"] * order["volume_orign"] * quote["volume_multiple"]  # 多头开仓成本
                position["position_cost_long"] -= position["position_price_long"] * order["volume_orign"] * quote["volume_multiple"]  # 多头持仓成本
            else:
                position["open_cost_short"] -= position["open_price_short"] * order["volume_orign"] * quote["volume_multiple"]  # 空头开仓成本
                position["position_cost_short"] -= position["position_price_short"] * order["volume_orign"] * quote["volume_multiple"]  # 空头持仓成本

            # 由 trade 引起的 account 原始字段变化，account 需要更新的计算字段
            premium = _get_premium(trade, quote)
            close_profit = _get_close_profit(trade, quote, position)
            self._adjust_account_by_trade(commission=trade["commission"], premium=premium, close_profit=close_profit)

            # 由 position 字段变化，同时 account 需要更新的计算字段
            buy_close = order["volume_orign"] if order["direction"] == "BUY" else 0
            sell_close = 0 if order["direction"] == "BUY" else order["volume_orign"]
            self._adjust_position_account(symbol, quote, underlying_quote, pre_last_price=position["last_price"],
                                          last_price=0, pre_underlying_last_price=position["underlying_last_price"],
                                          underlying_last_price=0, buy_close=buy_close, sell_close=sell_close)
        self._send_position(position)
        self._send_account()

    def _on_order_failed(self, symbol, order):
        origin_frozen_margin = order["frozen_margin"]
        origin_frozen_premium = order["frozen_premium"]
        order["frozen_margin"] = 0.0
        order["frozen_premium"] = 0.0
        self._send_order(order)

        # 调整账户和持仓
        if order["offset"] == 'OPEN':
            self._adjust_account_by_order(frozen_margin=-origin_frozen_margin, frozen_premium=-origin_frozen_premium)
            self._send_account()
        else:
            position = self._positions[symbol]
            if order["exchange_id"] in ["SHFE", "INE"]:
                if order["offset"] == "CLOSETODAY":
                    if order["direction"] == "BUY":
                        position["volume_short_frozen_today"] -= order["volume_orign"]
                    else:
                        position["volume_long_frozen_today"] -= order["volume_orign"]
                if order["offset"] == "CLOSE":
                    if order["direction"] == "BUY":
                        position["volume_short_frozen_his"] -= order["volume_orign"]
                    else:
                        position["volume_long_frozen_his"] -= order["volume_orign"]
            else:
                if order["direction"] == "BUY":
                    if position["volume_short_frozen_today"] >= order["volume_orign"]:
                        position["volume_short_frozen_today"] -= order["volume_orign"]
                    else:
                        position["volume_short_frozen_his"] -= order["volume_orign"] - position["volume_short_frozen_today"]
                        position["volume_short_frozen_today"] = 0
                else:
                    if position["volume_long_frozen_today"] >= order["volume_orign"]:
                        position["volume_long_frozen_today"] -= order["volume_orign"]
                    else:
                        position["volume_long_frozen_his"] -= order["volume_orign"] - position["volume_long_frozen_today"]
                        position["volume_long_frozen_today"] = 0
            self._adjust_position_volume_frozen(position)
            self._send_position(position)

    def _adjust_position_account(self, symbol, quote, underlying_quote=None, pre_last_price=float('nan'), last_price=float('nan'),
                                 pre_underlying_last_price=float('nan'), underlying_last_price=float('nan'),
                                 buy_open=0, buy_close=0, sell_open=0, sell_close=0):
        """
        价格变化，使得 position 中的以下计算字段需要修改，这个函数计算出需要修改的差值部分，计算出差值部分修改 position、account
        有两种情况下调用
        1. 委托单 FINISHED，且全部成交，分为4种：buy_open, buy_close, sell_open, sell_close
        2. 行情跳动
        """
        position = self._positions[symbol]
        float_profit_long = 0  # 多头浮动盈亏
        float_profit_short = 0  # 空头浮动盈亏
        position_profit_long = 0  # 多头持仓盈亏，期权持仓盈亏为0
        position_profit_short = 0  # 空头持仓盈亏，期权持仓盈亏为0
        margin_long = 0  # 多头占用保证金
        margin_short = 0  # 空头占用保证金
        market_value_long = 0  # 期权权利方市值(始终 >= 0)
        market_value_short = 0  # 期权义务方市值(始终 <= 0)
        assert [buy_open, buy_close, sell_open, sell_close].count(0) >= 3  # 只有一个大于0, 或者都是0，表示价格变化导致的字段修改
        if buy_open > 0:
            # 买开，pre_last_price 应该是成交价格，last_price 应该是 position['last_price']
            float_profit_long = (last_price - pre_last_price) * buy_open * quote["volume_multiple"]
            if quote["ins_class"].endswith("OPTION"):
                market_value_long = last_price * buy_open * quote["volume_multiple"]
            else:
                margin_long = buy_open * _get_future_margin(quote)
                position_profit_long = (last_price - pre_last_price) * buy_open * quote["volume_multiple"]
        elif sell_close > 0:
            # 卖平，pre_last_price 应该是 position['last_price']，last_price 应该是 0
            float_profit_long = -position["float_profit_long"] / position["volume_long"] * sell_close
            if quote["ins_class"].endswith("OPTION"):
                market_value_long = -pre_last_price * sell_close * quote["volume_multiple"]
            else:
                margin_long = -sell_close * _get_future_margin(quote)
                position_profit_long = -position["position_profit_long"] / position["volume_long"] * sell_close
        elif sell_open > 0:
            # 卖开
            float_profit_short = (pre_last_price - last_price) * sell_open * quote["volume_multiple"]
            if quote["ins_class"].endswith("OPTION"):
                market_value_short = -last_price * sell_open * quote["volume_multiple"]
                margin_short = sell_open * _get_option_margin(quote, last_price, underlying_last_price)
            else:
                margin_short = sell_open * _get_future_margin(quote)
                position_profit_short = (pre_last_price - last_price) * sell_open * quote["volume_multiple"]
        elif buy_close > 0:
            # 买平
            float_profit_short = -position["float_profit_short"] / position["volume_short"] * buy_close
            if quote["ins_class"].endswith("OPTION"):
                market_value_short = pre_last_price * buy_close * quote["volume_multiple"]
                margin_short = -buy_close * _get_option_margin(quote, pre_last_price, pre_underlying_last_price)
            else:
                margin_short = -buy_close * _get_future_margin(quote)
                position_profit_short = -position["position_profit_short"] / position["volume_short"] * buy_close
        else:
            float_profit_long = (last_price - pre_last_price) * position["volume_long"] * quote["volume_multiple"]  # 多头浮动盈亏
            float_profit_short = (pre_last_price - last_price) * position["volume_short"] * quote["volume_multiple"]  # 空头浮动盈亏
            if quote["ins_class"].endswith("OPTION"):
                margin_short = _get_option_margin(quote, last_price, underlying_last_price) * position["volume_short"] - position["margin_short"]
                market_value_long = (last_price - pre_last_price) * position["volume_long"] * quote["volume_multiple"]
                market_value_short = (pre_last_price - last_price) * position["volume_short"] * quote["volume_multiple"]
            else:
                # 期权持仓盈亏为 0
                position_profit_long = float_profit_long  # 多头持仓盈亏
                position_profit_short = float_profit_short  # 空头持仓盈亏
                margin_long = _get_future_margin(quote) * position["volume_long"] - position["margin_long"]
                margin_short = _get_future_margin(quote) * position["volume_short"] - position["margin_short"]

        if any([buy_open, buy_close, sell_open, sell_close]):
            # 修改 position volume 相关的计算字段
            # 在上面 sell_close buy_close 两种情况，计算浮动盈亏时，用到了修改前的手数，所以需改手数字段的代码放在这个位置
            self._adjust_position_volume(position)

        self._adjust_position(quote, position, float_profit_long, float_profit_short, position_profit_long,
                              position_profit_short, margin_long, margin_short, market_value_long, market_value_short)
        self._adjust_account_by_position(float_profit=float_profit_long + float_profit_short,
                                         position_profit=position_profit_long + position_profit_short,
                                         margin=margin_long + margin_short,
                                         market_value=market_value_long + market_value_short)

    # -------- 对于 position 的计算字段修改分为两类：
    # 1. 针对手数相关的修改，在下单、成交时会修改
    # 2. 针对盈亏、保证金、市值的修改，由于参考合约最新价，在成交、行情跳动时会修改

    def _adjust_position_volume_frozen(self, position):
        """position 原始字段修改后，只有冻结手数需要重新计算，有两种情况需要调用
        1. 下平仓单 2. 平仓单 FINISHED, 但没有成交
        """
        position["volume_long_frozen"] = position["volume_long_frozen_today"] + position["volume_long_frozen_his"]
        position["volume_short_frozen"] = position["volume_short_frozen_today"] + position["volume_short_frozen_his"]

    def _adjust_position_volume(self, position):
        """position 原始字段修改后，手数之后需要重新计算
        1. 委托单 FINISHED，且全部成交
        """
        position["pos_long_today"] = position["volume_long_today"]
        position["pos_long_his"] = position["volume_long_his"]
        position["pos_short_today"] = position["volume_short_today"]
        position["pos_short_his"] = position["volume_short_his"]
        position["volume_long"] = position["volume_long_today"] + position["volume_long_his"]
        position["volume_long_frozen"] = position["volume_long_frozen_today"] + position["volume_long_frozen_his"]
        position["volume_short"] = position["volume_short_today"] + position["volume_short_his"]
        position["volume_short_frozen"] = position["volume_short_frozen_today"] + position["volume_short_frozen_his"]

    def _adjust_position(self, quote, position, float_profit_long=0, float_profit_short=0, position_profit_long=0,
                                   position_profit_short=0, margin_long=0, margin_short=0, market_value_long=0,
                                   market_value_short=0):
        # 更新 position 计算字段，根据差值更新的字段
        position["float_profit_long"] += float_profit_long
        position["float_profit_short"] += float_profit_short
        position["position_profit_long"] += position_profit_long
        position["position_profit_short"] += position_profit_short
        position["margin_long"] += margin_long
        position["margin_short"] += margin_short
        position["market_value_long"] += market_value_long
        position["market_value_short"] += market_value_short

        # 更新 position 计算字段，原地重新计算的字段
        if position["volume_long"] > 0:
            position["open_price_long"] = position["open_cost_long"] / position["volume_long"] / quote["volume_multiple"]
            position["position_price_long"] = position["position_cost_long"] / position["volume_long"] / quote["volume_multiple"]
        else:
            position["open_price_long"] = float("nan")
            position["position_price_long"] = float("nan")
        if position["volume_short"] > 0:
            position["open_price_short"] = position["open_cost_short"] / position["volume_short"] / quote["volume_multiple"]
            position["position_price_short"] = position["position_cost_short"] / position["volume_short"] / quote["volume_multiple"]
        else:
            position["open_price_short"] = float("nan")
            position["position_price_short"] = float("nan")
        position["float_profit"] = position["float_profit_long"] + position["float_profit_short"]
        position["position_profit"] = position["position_profit_long"] + position["position_profit_short"]
        position["margin"] = position["margin_long"] + position["margin_short"]
        position["market_value"] = position["market_value_long"] + position["market_value_short"]

    # -------- 对于 account 的修改分为以下三类

    def _adjust_account_by_trade(self, commission=0, close_profit=0, premium=0):
        """由成交引起的 account 原始字段变化，account 需要更新的计算字段"""
        # account 原始字段
        self._account["close_profit"] += close_profit
        self._account["commission"] += commission
        self._account["premium"] += premium  # premium变量的值有正负，正数表示收入的权利金，负数表示付出的权利金
        # account 计算字段
        self._account["balance"] += close_profit - commission + premium
        self._account["available"] += close_profit - commission + premium
        self._account["risk_ratio"] = self._account["margin"] / self._account["balance"]

    def _adjust_account_by_position(self, float_profit=0, position_profit=0, margin=0, market_value=0):
        """由 position 变化，account 需要更新的计算字段"""
        # account 计算字段，持仓字段求和的字段
        self._account["float_profit"] += float_profit
        self._account["position_profit"] += position_profit
        self._account["margin"] += margin
        self._account["market_value"] += market_value
        # account 计算字段
        self._account["balance"] += position_profit + market_value
        self._account["available"] += position_profit - margin
        self._account["risk_ratio"] = self._account["margin"] / self._account["balance"]

    def _adjust_account_by_order(self, frozen_margin=0, frozen_premium=0):
        """由 order 变化，account 需要更新的计算字段"""
        self._account["frozen_margin"] += frozen_margin
        self._account["frozen_premium"] += frozen_premium
        self._account["available"] -= (frozen_margin + frozen_premium)

    def _send_trade(self, trade):
        self._diffs.append({
            "trade": {
                self._account_key: {
                    "trades": {
                        trade["trade_id"]: trade.copy()
                    }
                }
            }
        })

    def _send_order(self, order):
        self._diffs.append({
            "trade": {
                self._account_key: {
                    "orders": {
                        order["order_id"]: order.copy()
                    }
                }
            }
        })

    def _send_position(self, position):
        self._diffs.append({
            "trade": {
                self._account_key: {
                    "positions": {
                        position["exchange_id"] + "." + position["instrument_id"]: position.copy()
                    }
                }
            }
        })

    def _send_account(self):
        self._diffs.append({
            "trade": {
                self._account_key: {
                    "accounts": {
                        "CNY": self._account.copy()
                    }
                }
            }
        })

    def _default_get_trade_timestamp(self):
        """获取交易时间的默认方法，为当前所有 quote 的最大行情时间"""
        return int(datetime.strptime(self._max_datetime, "%Y-%m-%d %H:%M:%S.%f").timestamp() * 1e6) * 1000

    def _default_is_in_trading_time(self, quote):
        """判断是否在交易时间段"""
        return _is_in_trading_time(quote, self._max_datetime, float("nan"))
