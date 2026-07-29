"""Market data implementation methods for stocks, indexes, and futures."""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, overload
from uuid import UUID

from robinhood.constants import (
    API_CURRENCY_QUOTES,
    API_FUTURES_CONTRACTS,
    API_FUTURES_PRODUCTS,
    API_FUTURES_QUOTES,
    API_INDEX_QUOTE,
    API_INDEXES,
    API_INSTRUMENTS,
    API_OPTIONS_GREEKS_DATA,
    API_ORDERBOOK,
    API_QUOTES,
    DATA,
    PARAM_ID,
    PARAM_PRODUCT_IDS,
    PARAM_SYMBOLS,
    STATUS,
    SUCCESS,
)
from robinhood.core._typing_base import TypingBase
from robinhood.dataclasses.api_dataclasses import (
    CurrencyQuote,
    FuturesContract,
    FuturesProduct,
    FuturesQuote,
    IndexInfo,
    IndexQuote,
    InstrumentQuote,
    OptionGreekData,
    OrderBook,
    StockInfo,
)
from robinhood.robinhood_errors import (
    InvalidTypeError,
    NoFutureProductsReturnedError,
)
from robinhood.utils._normalize_symbol import (
    check_if_uuid4,
    normalize_currency_input,
    normalize_future_input,
    uppercase_input,
)
from robinhood.utils.types import StrWatchListItem

logger = logging.getLogger(__name__)


class MarketDataImpl(TypingBase):
    """Mixin containing stock, index, order book, and futures requests."""

    @overload
    async def _get_stock_info(self, symbols: str) -> StockInfo | None: ...

    @overload
    async def _get_stock_info(
        self, symbols: list[str]
    ) -> list[StockInfo] | None: ...

    async def _get_stock_info(
        self, symbols: str | list[str]
    ) -> StockInfo | list[StockInfo] | None:
        """
        [Public]
        Return stock metadata for one symbol or a list of symbols.
        """
        symbols = uppercase_input(symbols)
        _symbols = symbols
        if isinstance(symbols, list):
            symbols = ",".join(symbols)
        res_json = await self._async_http_client._get(
            endpoint=API_INSTRUMENTS,
            params={PARAM_SYMBOLS: symbols},
        )
        if not res_json:
            return None
        stock_info_list = [StockInfo.from_json(r) for r in res_json if r]
        if self._db_cache:
            for s in stock_info_list:
                self._db_cache.insert_stock_info(s)
        if isinstance(_symbols, str):
            return stock_info_list[0]
        return stock_info_list

    @overload
    async def _get_index_info(self, symbols: str) -> IndexInfo | None: ...

    @overload
    async def _get_index_info(
        self, symbols: list[str]
    ) -> list[IndexInfo] | None: ...

    async def _get_index_info(
        self,
        symbols: str | list[str],
    ) -> list[IndexInfo] | IndexInfo | None:
        """
        [Public]
        Return index metadata for one symbol or a list of symbols.
        """
        symbols = uppercase_input(symbols)
        if isinstance(symbols, list):
            symbols = ",".join(symbols)
        params = {PARAM_SYMBOLS: symbols}
        res_json = await self._async_http_client._get(
            endpoint=API_INDEXES,
            params=params,
        )
        if not res_json:
            return None
        indexes = [IndexInfo.from_json(i) for i in res_json if i]
        if not indexes:
            return None
        return indexes if len(indexes) > 1 else indexes[0]

    @overload
    async def _get_index_quotes(self, symbols: str) -> IndexQuote | None: ...

    @overload
    async def _get_index_quotes(
        self, symbols: list[str]
    ) -> list[IndexQuote] | None: ...

    async def _get_index_quotes(
        self, symbols: str | list[str]
    ) -> list[IndexQuote] | IndexQuote | None:
        """
        [Public]
        Returns IndexQuote classes for one symbol or a list of symbols
        """
        symbols = uppercase_input(symbols)
        if isinstance(symbols, list):
            symbols = ",".join(symbols)
        params = {PARAM_SYMBOLS: symbols}
        res_json = await self._async_http_client._get(
            endpoint=API_INDEX_QUOTE, params=params
        )
        if not res_json:
            return None
        index_quotes = []
        for i in res_json[0]["data"]:
            if i:
                try:
                    index_quotes.append(IndexQuote.from_json(i["data"]))
                except KeyError:
                    continue
        if not index_quotes:
            return None
        return index_quotes if len(index_quotes) > 1 else index_quotes[0]

    @overload
    async def _get_stock_quotes(
        self, symbol: str
    ) -> InstrumentQuote | None: ...

    @overload
    async def _get_stock_quotes(
        self, symbol: list[str]
    ) -> list[InstrumentQuote] | None: ...

    async def _get_stock_quotes(
        self, symbol: list[str] | str
    ) -> InstrumentQuote | list[InstrumentQuote] | None:
        """
        [Public]
        Returns a list of InstrumentQuote dataclasses
        Ensure symbols are capitalized
        """
        symbol = uppercase_input(symbol)
        symbol = [symbol] if isinstance(symbol, str) else symbol
        joined_symbol = ",".join(symbol)
        res_json = await self._async_http_client._get(
            endpoint=API_QUOTES, params={PARAM_SYMBOLS: joined_symbol}
        )
        return_val = [InstrumentQuote.from_json(r) for r in res_json if r]
        if not res_json:
            return None
        return return_val if len(return_val) > 1 else return_val[0]

    async def _get_orderbook(self, symbol: str) -> OrderBook | None:
        """
        [Public]
        Return an OrderBook dataclass
        Userful attributes:
        - OrderBook.asks
        - OrderBook.bids
        Both are lists of BidAsk dataclasses
        that contain the price, quanity, and side
        """
        symbol = uppercase_input(symbol)
        si = await self._get_stock_info(symbol)
        if not si:
            logger.warning("%s returned none", symbol)
            return None
        res_json = await self._async_http_client._get(
            API_ORDERBOOK + f"{si.id}/"
        )
        if not res_json:
            logger.warning("%s returned none", symbol)
            return None
        return OrderBook.from_json(res_json[0])

    # A note on the futures endpoint due to how Robinhood
    # handles params and filtering single symbol look-ups
    # should prob be used...
    # This endpoint is complete garbage nested return vals😾

    # no need to raise ValueError if incorrect type is used
    # some faith in the user is required...
    async def _get_future_info(self, symbol: str) -> FuturesProduct | None:
        """
        [Public]
        This is a convience function that calls `get_all_futures_products`
        and filters for the symbols.
        Symbols should be as follows:
        /ES not specific future contracts like /ESM26,
        Forward slash is not required
        """
        symbol = normalize_future_input(symbol)
        futures_prods = await self._get_all_futures_products()
        if not futures_prods:
            # get_all_futures_products has a warning logger already
            # if no reponse back
            return None
        for i, f in enumerate(futures_prods):
            if f.displaySymbol.startswith(symbol):
                return futures_prods[i]
        logger.warning("%s was not found", symbol)
        return None

    @overload
    async def _get_future_quote(
        self,
        ids: str,
    ) -> FuturesQuote | None: ...

    @overload
    async def _get_future_quote(
        self,
        ids: list[str],
    ) -> list[FuturesQuote] | None: ...

    async def _get_future_quote(
        self,
        ids: str | list[str],
    ) -> list[FuturesQuote] | FuturesQuote | None:
        """
        [Public]
        Accepts either exact symbols such as /ESM26 or actual contract id
        Using exact symbols has some overhead costs,
        suggested to use contract ids.
        Use exact symbol name ex: /ESM26, forward slash is optional
        """
        if isinstance(ids, str):
            ids = [ids]
        if not check_if_uuid4(ids):
            ids = normalize_future_input(ids)
            mapping = await self._resolve_symbol_to_id(ids)
            ids = list(mapping.values())
        if len(ids) > 20:
            raise ValueError("max amount of ids is 20")
        params = {PARAM_ID: ",".join(ids)}
        res_json = await self._async_http_client._get(
            endpoint=API_FUTURES_QUOTES,
            params=params,
        )
        if not res_json or not (quote_list := res_json[0][DATA]):
            logger.warning("No valid quotes were returned for ids")
            return None
        quotes: list[FuturesQuote] = []
        for n in quote_list:
            if n[STATUS] != SUCCESS:
                continue
            quotes.append(FuturesQuote.from_json(n[DATA]))
        if not quotes:
            return None
        return quotes if len(quotes) > 1 else quotes[0]

    async def _resolve_symbol_to_id(
        self, symbols: str | list[str]
    ) -> dict[str, str]:
        """
        [Private]
        Resolve future symbols such as /ESM26 to active contract id
        """
        all_futures = await self._get_all_futures_products()
        mapping_symbols_to_ids: dict[str, str] = {}
        if not all_futures:
            raise NoFutureProductsReturnedError
        if isinstance(symbols, str):
            for s in all_futures:
                if s.displaySymbol in symbols:
                    mapping_symbols_to_ids[symbols] = s.activeFuturesContractId
            return mapping_symbols_to_ids
        if isinstance(symbols, list):
            for s in all_futures:
                for sy in symbols:
                    if s.displaySymbol in sy:
                        logger.debug("Found %s for %s", s.displaySymbol, sy)
                        mapping_symbols_to_ids[sy] = s.activeFuturesContractId
            return mapping_symbols_to_ids

    async def _get_all_futures_products(
        self,
    ) -> list[FuturesProduct] | None:
        """
        [Public]
        Return a list of all Futures Products
        """
        res_json = await self._async_http_client._get(API_FUTURES_PRODUCTS)
        if not res_json:
            logger.warning("Unable to retrieve futures products list")
            return None
        futures = [FuturesProduct.from_json(f) for f in res_json if f]
        # In the event of a completly empty list return None
        # though this shouldn't really matter
        # and I should prob change this for all return values later
        # Just leaving this here as a placeholder/reminder
        return None if not futures else futures

    async def _get_active_contracts_for_id(
        self, id: str
    ) -> list[FuturesContract] | None:
        """
        [Public]
        Take a future product id and returns all active contracts
        Sorted by earliest expiration
        Contract id can be found from a FuturesProduct's id var
        """
        params = {PARAM_PRODUCT_IDS: id}
        res_json = await self._async_http_client._get(
            endpoint=API_FUTURES_CONTRACTS,
            params=params,
        )
        if not res_json:
            logger.warning("No contracts returned for %s", id)
            return None
        future_contracts = [FuturesContract.from_json(f) for f in res_json if f]
        future_contracts.sort(key=lambda fut: fut.expirationMmy)
        return future_contracts

    async def _get_currency_quote(self, symbol: str) -> CurrencyQuote | None:
        """
        [Public]
        Return a CurrencyQuote dataclass for a symbol such as `BTC, DOGE, EUR`
        Value is denoted in USD
        """
        symbol = normalize_currency_input(symbol)
        params = {PARAM_SYMBOLS: symbol}
        res_json = await self._async_http_client._get(
            endpoint=API_CURRENCY_QUOTES,
            params=params,
        )
        if not res_json or not res_json[0]:
            return None
        return CurrencyQuote.from_json(res_json[0])

    async def __currency_id_check(self, _id: str) -> CurrencyQuote | None:
        """
        [Private]
        Only accepts UUID
        Not a fully fleshed out endpoint yet
        Just for the check input type function
        """
        params = {PARAM_ID: _id}
        res_json = await self._async_http_client._get(
            endpoint=API_CURRENCY_QUOTES,
            params=params,
        )
        if not res_json or not res_json[0]:
            return None
        return CurrencyQuote.from_json(res_json[0])

    async def __instrument_id_check(self, _id: str) -> StockInfo | None:
        """
        [Private]
        """
        params = {PARAM_ID: _id}
        res_json = await self._async_http_client._get(
            endpoint=API_INSTRUMENTS, params=params
        )
        if not res_json or not res_json[0]:
            return None
        return StockInfo.from_json(res_json[0])

    async def __option_id_check(self, _id: str) -> OptionGreekData | None:
        """
        [Private]
        """
        params = {PARAM_ID: _id}
        res_json = await self._async_http_client._get(
            endpoint=API_OPTIONS_GREEKS_DATA, params=params
        )
        if not res_json or not res_json[0]:
            return None
        return OptionGreekData.from_json(res_json[0])

    async def __index_id_check(self, _id: str) -> IndexInfo | None:
        """
        [Private]
        """
        params = {PARAM_ID: _id}
        res_json = await self._async_http_client._get(
            endpoint=API_INDEXES, params=params
        )
        if not res_json or not res_json[0]:
            return None
        return IndexInfo.from_json(res_json[0])

    # only way i can think of to check item type in robinhood
    # is to call each end-point
    async def __resolve_str_repr_to_id(
        self, item: str
    ) -> tuple[StrWatchListItem, str] | None:
        """
        [Private]
        """
        str_callables: list[Callable[[str], Awaitable[object | None]]] = [
            self._get_stock_quotes,
            self._get_index_quotes,
            self._get_future_quote,
            self._get_currency_quote,
        ]
        results = await asyncio.gather(
            *[f(item) for f in str_callables],
            return_exceptions=True,
        )
        if not results:
            raise InvalidTypeError(f"{item} is not valid type")
        items = [r for r in results if r and not isinstance(r, Exception)]
        # totally not hacky fix :)
        if "-" in item and len(items) >= 2:
            items.pop(0)
        final_item = items[0]
        if isinstance(final_item, InstrumentQuote):
            if self._db_cache:
                self._db_cache.insert_object_info(
                    final_item.instrument_id,
                    "instrument",
                    final_item.symbol,
                )
            return "instrument", final_item.instrument_id
        if isinstance(final_item, IndexQuote):
            if self._db_cache:
                self._db_cache.insert_object_info(
                    final_item.instrument_id,
                    "index",
                    final_item.symbol,
                )
            return "index", final_item.instrument_id
        if isinstance(final_item, FuturesQuote):
            if self._db_cache:
                self._db_cache.insert_object_info(
                    final_item.instrument_id,
                    "future",
                    final_item.symbol,
                )
            return "future", final_item.instrument_id
        if isinstance(final_item, CurrencyQuote):
            if self._db_cache:
                self._db_cache.insert_object_info(
                    final_item.id,
                    "currency_pair",
                    final_item.symbol,
                )
            return "currency_pair", final_item.id

    async def __resolve_UUID_repr_to_id(
        self, item: UUID
    ) -> tuple[StrWatchListItem, str] | None:
        """
        [Private]
        """
        checks: list[Callable[[str], Awaitable[object | None]]] = [
            # Future quote endpoint already works with UUID
            # doesnt need its own check function
            self._get_future_quote,
            self.__index_id_check,
            self.__currency_id_check,
            self.__instrument_id_check,
            self.__option_id_check,
        ]
        if self._db_cache:
            self._db_cache.fetch_rh_object(item)
        results = await asyncio.gather(
            *[c(str(item)) for c in checks],
            return_exceptions=True,
        )
        final_type_li = [
            r for r in results if r and not isinstance(r, Exception)
        ]
        if not final_type_li:
            return None
        else:
            final_type_li = final_type_li[0]
        if isinstance(final_type_li, StockInfo):
            if self._db_cache:
                self._db_cache.insert_object_info(
                    str(item),
                    "instrument",
                    final_type_li.symbol,
                )
            return "instrument", str(item)
        if isinstance(final_type_li, IndexInfo):
            if self._db_cache:
                self._db_cache.insert_object_info(
                    str(item),
                    "index",
                    final_type_li.symbol,
                )
            return "index", str(item)
        if isinstance(final_type_li, FuturesQuote):
            if self._db_cache:
                self._db_cache.insert_object_info(
                    str(item),
                    "future",
                    final_type_li.symbol,
                )
            return "future", str(item)
        if isinstance(final_type_li, CurrencyQuote):
            if self._db_cache:
                self._db_cache.insert_object_info(
                    str(item),
                    "currency_pair",
                    final_type_li.symbol,
                )
            return "currency_pair", str(item)
        if isinstance(final_type_li, OptionGreekData):
            if self._db_cache:
                self._db_cache.insert_object_info(
                    str(item),
                    "option_strategy",
                    final_type_li.symbol,
                )
            return "option_strategy", str(item)
        return None

    async def _check_input_type(
        self,
        item: str | UUID,
    ) -> tuple[StrWatchListItem, str] | None:
        """
        [Private]
        Note: for option_strategies can only be modified with UUID.
        """
        if self._db_cache:
            result = self._db_cache.fetch_rh_object(item)
            if result:
                return result
        if isinstance(item, str):
            result = await self.__resolve_str_repr_to_id(item)
            logger.debug("String resolve result: %s", result)
            return result
        if isinstance(item, UUID):
            result = await self.__resolve_UUID_repr_to_id(item)
            logger.debug("UUID resolve result: %s", result)
            return result

    async def _get_all_instruments(self) -> list[StockInfo] | None:
        # TODO: Add caching later...
        """
        [Public]
        (Warning) This function is expensive to run
        Returns all stocks on robinhood includes untradable stocks
        """
        res_json = await self._async_http_client._get(API_INSTRUMENTS)
        stock_info_list = [StockInfo.from_json(s) for s in res_json if s]
        if not stock_info_list:
            # this should never return 0 items
            return None
        if self._db_cache:
            for s in stock_info_list:
                self._db_cache.insert_stock_info(s)
        return stock_info_list
