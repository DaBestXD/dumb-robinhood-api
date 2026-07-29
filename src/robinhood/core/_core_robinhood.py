"""Shared implementation used by the sync and async public clients."""

from __future__ import annotations

import asyncio

#  __    __     ______     ______     __     __
# /\ "-./  \   /\  ___\   /\  __ \   /\ \  _ \ \
# \ \ \-./\ \  \ \  __\   \ \ \/\ \  \ \ \/ ".\ \
#  \ \_\ \ \_\  \ \_____\  \ \_____\  \ \__/".~\_\
#   \/_/  \/_/   \/_____/   \/_____/   \/_/   \/_/
#  __    __     ______     ______     __     __
# /\ "-./  \   /\  ___\   /\  __ \   /\ \  _ \ \
# \ \ \-./\ \  \ \  __\   \ \ \/\ \  \ \ \/ ".\ \
#  \ \_\ \ \_\  \ \_____\  \ \_____\  \ \__/".~\_\
#   \/_/  \/_/   \/_____/   \/_____/   \/_/   \/_/
#  __  __     ______     ______     _____
# /\ \_\ \   /\  __ \   /\  __ \   /\  __-.
# \ \  __ \  \ \ \/\ \  \ \ \/\ \  \ \ \/\ \
#  \ \_\ \_\  \ \_____\  \ \_____\  \ \____-
#   \/_/\/_/   \/_____/   \/_____/   \/____/
# 🐈
import logging
import os
from pathlib import Path
from typing import Any, Coroutine

from robinhood.browser_functions.browser_token_parser import (
    Browser,
    Chrome,
)
from robinhood.browser_functions.token_functions import (
    _refresh_access_token,
    _return_access_token_expiry,
)
from robinhood.core._account_impl import AccountImpl
from robinhood.core._http_async_client import RobinhoodAsyncHTTPClient
from robinhood.core._market_data_impl import MarketDataImpl
from robinhood.core._option_impl import OptionsImpl
from robinhood.core._trading_impl import TradingImpl
from robinhood.db_logic.option_cache import OptionCache
from robinhood.robinhood_errors import TokenExtractionError
from robinhood.utils.configure_logger import _MISSING, MISSING, configure_logger
from robinhood.utils.set_up_script import set_up
from robinhood.utils.types import T

logger = logging.getLogger(__name__)


class _CoreRobinhood(
    MarketDataImpl,
    AccountImpl,
    OptionsImpl,
    TradingImpl,
):
    def __init__(
        self,
        *,
        config_path: os.PathLike[str] | Path = Path.cwd(),
        user_agent: str | None = None,
        enable_cache: bool = True,
        prune_expired_options: bool = True,
        logging_level: int | None = logging.INFO,
        log_handler: logging.Handler | None | _MISSING = MISSING,
        browser_type: type[Browser] = Chrome,
    ) -> None:
        """
        Initialize shared auth, cache, logging, and HTTP-client state.

        This base class powers both `Robinhood` and `AsyncRobinhood`. Public
        users normally instantiate one of those concrete clients.
        Attempts to load token from env and checks if it's still valid

        Raises TokenExtractionError if token is unable to be found. If this
        occurs try logging into robinhood on the browser then closing.
        """
        configure_logger(logging_level, log_handler)
        self.browser_type = browser_type()
        token = self.browser_type._extracted_token
        if not token:
            raise TokenExtractionError("Bearer token cannot be none.")
        self.acc_id = self.browser_type.acc_id
        if enable_cache:
            config_dir = set_up(Path(config_path))
            cache_path = config_dir / "meow-meow-hood.db"
            self._db_cache = OptionCache(cache_path, prune_expired_options)
        else:
            logger.info("Cache is disabled skipping config setup")
            self._db_cache = None
        # set up http client
        self._async_http_client = RobinhoodAsyncHTTPClient(token, user_agent)
        # begin event loop for both async/sync versions
        self.event_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        logger.info("Robinhood Client Initialized")

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        return self.event_loop.run_until_complete(coro)

    def refresh_access_token(
        self,
        browser: type[Browser] | None = None,
        *,
        time_until_close: int = 10,
        auto_open_browser: bool = True,
        headless: bool = True,
    ) -> None:
        """
        (Warning) If browser is open this function will break, and raise
        RuntimeError.

        For param 'browser' allows for manually override if you wish
        to change which browser the class uses.
        If no browser is provided will use the browser that was
        used on initilization(default is chrome).

        Opens browser if last access timed is older than 1 day,
        if the lass access time is older than 7 days raises hard error,
        and you will need to manually log back in.

        If token is expired will open the browser to retrieve new token

        """
        self.browser_type = self.browser_type if not browser else browser()
        if (
            auto_open_browser
            and self.browser_type.last_accessed_greater_than_n_days()
        ):
            self.browser_type.open_and_close_browser(
                time_until_close=time_until_close,
                headless=headless,
            )
        access_token = self._async_http_client.access_token
        token = _refresh_access_token(access_token, self.browser_type)
        if isinstance(token, str):
            self._async_http_client.update_session_token(token)
            self._async_http_client.access_token = token
            self._async_http_client.session = None
        return None

    def get_access_token_expiry(self) -> int:
        """
        Return the expiration from the currently loaded access_token
        as an int timestamp
        """
        return _return_access_token_expiry(self._async_http_client.access_token)

    def prune_db(self) -> None:
        """
        Removes all expired information relating to options,
        from the following tables:
        - expiration_dates
        - option_Ids
        - option_expiration_sync
        """
        if not self._db_cache:
            logger.warning("No db cache, nothing to prune")
            return None
        self._db_cache.prune_expired()
        return None

    def execute_custom_sql(
        self, query: str, args: list[dict[str, Any] | dict[str, Any]]
    ) -> list[Any] | None:
        """
        Use if you need to execute a custom sql query
        Use a list of args if you need to use ``executemany()``
        """
        if not self._db_cache:
            logger.warning("No db enabled! Nothing to execute!")
            return None
        else:
            logger.debug("Exexuting %s with args %s", query, args)
            return self._db_cache.execute_query_with_args(query, args)
