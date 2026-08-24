from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
from enum import StrEnum
from pathlib import Path
from typing import Self

import snappy

from robinhood.browser_functions.browser_utils import (
    HOME_DIR,
    build_popen_kwargs,
    check_file_stat,
    is_token_valid,
)

logger = logging.getLogger(__name__)


class FirefoxPaths(StrEnum):
    MAC = str(HOME_DIR / Path("Library/Application Support/Firefox/Profiles/"))
    WINDOWS = str(HOME_DIR / Path("AppData/Roaming/Mozilla/Firefox/Profiles/"))
    LINUX = str(HOME_DIR / Path(".mozilla/firefox/"))
    DB_PATH = str("storage/default/https+++robinhood.com/ls/data.sqlite")
    MAC_APPLICATION_PATH = "/Applications/Firefox.app/Contents/MacOS/firefox"
    WINDOWS_APPLICATION_PATH = r"C:\Program Files\Mozilla Firefox\firefox.exe"
    LINUX_APPLICATION_PATH = "/usr/bin/firefox"


class Firefox:
    auth_path: Path
    application_path: Path
    __slots__ = {"auth_path", "application_path"}

    @classmethod
    def browser_factory(cls) -> Self:
        firefox = cls()
        match sys.platform:
            case "win32":
                firefox.auth_path = Firefox.find_profile_path(
                    str(FirefoxPaths.WINDOWS)
                )
                firefox.application_path = Path(
                    FirefoxPaths.WINDOWS_APPLICATION_PATH
                )
            case "darwin":
                firefox.auth_path = Firefox.find_profile_path(
                    str(FirefoxPaths.MAC)
                )
                firefox.application_path = Path(
                    FirefoxPaths.MAC_APPLICATION_PATH
                )
            case "linux":
                firefox.auth_path = Firefox.find_profile_path(
                    str(FirefoxPaths.LINUX)
                )
                firefox.application_path = Path(
                    FirefoxPaths.LINUX_APPLICATION_PATH
                )
            case _:
                raise ValueError(f"Unsupported platform {sys.platform}")
        return firefox

    @classmethod
    def manual_override_factory(
        cls, path: str | Path, application_path: str | Path
    ) -> Self:
        """
        Use this to resolve multiple path conflicts
        """
        firefox = cls()
        firefox.auth_path = Path(path)
        firefox.application_path = Path(application_path)
        return firefox

    def get_token(self, check_token: bool = False) -> str | None:
        """
        If check_token is true will test if token is valid and
        if token is invalid will return None
        """
        db_file_path = f"file:{self.auth_path}?immutable=1"
        con = sqlite3.connect(db_file_path, uri=True)
        bearer_access_check = None
        try:
            cur = con.cursor()
            cur.execute("SELECT value FROM data WHERE key = 'web:auth_state'")
            bearer_access_check: tuple[bytes] | None = cur.fetchone()
        except sqlite3.OperationalError as e:
            logger.warning("%s", e)
        finally:
            con.close()
        if bearer_access_check is None:
            return None
        blob = snappy.decompress(bearer_access_check[0])
        auth_dict: dict[str, str] = json.loads(blob.decode())
        access_token = auth_dict["access_token"]
        if check_token and not is_token_valid(access_token):
            return None
        else:
            return access_token

    @staticmethod
    def _return_env_vals(
        manual_overide: dict[str, str] | None = None,
    ) -> dict[str, str]:
        env_vars = os.environ.copy()
        if manual_overide:
            env_vars.update(manual_overide)
            return env_vars
        else:
            env_vars.update(
                {
                    "MOZ_HEADLESS": "1",
                    "MOZ_DISABLE_GPU": "1",
                    "MOZ_WEBRENDER": "0",
                }
            )
            return env_vars

    @staticmethod
    def _is_open() -> bool:
        if sys.platform == "win32":
            check = subprocess.run(
                ["tasklist", "/fi", "imagename eq firefox.exe"],
                capture_output=True,
                text=True,
            )
            return "no tasks" not in check.stdout.lower()

        else:
            check = subprocess.run(
                ["pgrep", "-fli", "FIREFOX"],
                capture_output=True,
            )
            return check.returncode == 0

    def build_cmd_args(self, headless: bool) -> list[str]:
        path_seq = self.auth_path.parents
        if sys.platform == "linux":
            profile_path = [
                path_seq[i - 1]
                for i, f in enumerate(path_seq)
                if f.name == "firefox"
            ]
        else:
            profile_path = [
                path_seq[i - 1]
                for i, f in enumerate(path_seq)
                if f.name == "Profiles"
            ]
        if len(profile_path) != 1:
            raise ValueError
        profile_path = profile_path[0]
        args = [str(self.application_path)]
        if headless:
            args.extend(["-headless", "-no-remote"])
        args.extend(["-profile", str(profile_path)])
        args.append("https://robinhood.com")
        return args

    def open_and_close_browser(
        self,
        retries: int = 3,
        close_after_seconds: float = 10,
        headless: bool = True,
    ) -> None:
        """
        TODO: docstring
        """
        args = self.build_cmd_args(headless)
        logger.debug(f"Pre open time: {check_file_stat(self.auth_path):2f}")
        if Firefox._is_open():
            raise RuntimeError("Firefox is already open, close and run again")
        proc = None
        env_vars: dict[str, str] = (
            Firefox._return_env_vals() if headless else os.environ.copy()
        )
        for _ in range(retries):
            try:
                proc = subprocess.Popen(
                    args,
                    env=env_vars,
                    **build_popen_kwargs(),
                )
                break
            except BlockingIOError:
                logger.debug("BlockingIOError retrying...")
        if proc is None:
            raise RuntimeError("BlockingIOError occured past retry limit")
        try:
            time.sleep(close_after_seconds)
        finally:
            self.close_process(proc)
        logger.debug(f"Post open time: {check_file_stat(self.auth_path):2f}")

    def close_process(
        self,
        proc: subprocess.Popen[bytes],
        timeout: float = 5,
    ) -> None:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/IM", "firefox.exe"])
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                logger.debug("Timeout hit force killing process")
                subprocess.run(["taskkill", "/IM", "firefox.exe", "/T", "/F"])
                proc.wait()
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                proc.poll()
                logger.debug(
                    "Process %d already exited with %s",
                    proc.pid,
                    proc.returncode,
                )
                return

            try:
                proc.wait(timeout)
                return
            except subprocess.TimeoutExpired:
                logger.debug(
                    "SIGTERM timed out for process group %d",
                    proc.pid,
                )

            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()

    @staticmethod
    def find_profile_path(path: str) -> Path:
        """
        This function only returns the path if a robinhood folder is found,
        if multiple robinhood folder is found raises value error.
        """
        profiles = (f for f in Path(path).iterdir() if f.is_dir())
        robinhood_folder_found_list = [
            p / FirefoxPaths.DB_PATH
            for p in profiles
            if (p / FirefoxPaths.DB_PATH).exists()
        ]
        if len(robinhood_folder_found_list) > 1:
            raise RuntimeError("Multiple robinhood profiles found!")

        if len(robinhood_folder_found_list) == 0:
            raise RuntimeError(
                "No valid profile was found make sure you are logged into robinhood on Firefox!"  # noqa: E501
            )
        return robinhood_folder_found_list[0]
