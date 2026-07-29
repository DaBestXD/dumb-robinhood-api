from __future__ import annotations

import base64
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NotRequired, TypedDict, cast

import requests

from robinhood.constants import API_ACCOUNT, BASE_API_LINK

HOME_DIR = Path.home()
logger = logging.getLogger(__name__)


class _RobinhoodJWT(TypedDict):
    """
    Not a full list of all items in the returned jwt, most values
    returned have little value
    """

    device_hash: str
    exp: int
    """
    Token expiration
    """
    user_id: str
    """
    UUID4
    """


def decode_jwt(token: str) -> _RobinhoodJWT:
    payload_b64 = token.split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    decoded_payload: dict[str, Any] = json.loads(
        base64.urlsafe_b64decode(payload_b64).decode()
    )
    return cast(_RobinhoodJWT, decoded_payload)


def is_token_valid(token: str, retry_amount: int = 3) -> bool:
    """
    Check expiration of token, then perform a get request to test token
    """
    jwt = decode_jwt(token)
    if jwt["exp"] < time.time():
        return False
    for _ in range(retry_amount):
        response = requests.get(
            BASE_API_LINK + API_ACCOUNT,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if response.status_code >= 500:
            continue
    return response.status_code == 200


def check_file_stat(file_to_check: Path) -> float:
    """
    Check the file stat for the auth path, and returns
    the number of days the files has been last accessed
    """
    last_accesssed = file_to_check.stat().st_mtime
    days_since_last_accessed = (last_accesssed - time.time()) / (86000)
    return abs(days_since_last_accessed)


class _PopenPlatformKwargs(TypedDict):
    creationflags: NotRequired[int]
    start_new_session: NotRequired[bool]


def build_popen_kwargs() -> _PopenPlatformKwargs:
    popen_dict_kwargs: _PopenPlatformKwargs = {}
    if sys.platform == "win32":
        popen_dict_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_dict_kwargs["start_new_session"] = True
    return popen_dict_kwargs
