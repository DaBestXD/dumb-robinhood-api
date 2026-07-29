import logging
import os
import re
import signal
import subprocess
import sys
import time
from enum import StrEnum
from pathlib import Path
from typing import Self

from robinhood.browser_functions.browser_utils import (
    build_popen_kwargs,
    check_file_stat,
    decode_jwt,
    is_token_valid,
)

HOME_DIR = Path.home()
logger = logging.getLogger(__name__)


class ChromePaths(StrEnum):
    DB_PATH = "https_robinhood.com_0.indexeddb.leveldb"
    MAC = str(
        HOME_DIR
        / Path("Library/Application Support/Google/Chrome/Default/IndexedDB")
        / DB_PATH
    )
    WINDOWS = str(
        HOME_DIR
        / Path("AppData/Local/Google/Chrome/User Data/Default/IndexedDB")
        / DB_PATH
    )
    LINUX = str(
        HOME_DIR / Path(".config/google-chrome/Default/IndexedDB") / DB_PATH
    )
    MAC_APP_PATH = (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )
    WINDOWS_APP_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    LINUX_APP_PATH = "/usr/bin/google-chrome"


class Chrome:
    auth_path: Path
    application_path: Path
    profile_dir_name: str
    __slots__ = {"auth_path", "application_path", "profile_dir_name"}

    @classmethod
    def browser_factory(cls) -> Self:
        chrome = cls()
        chrome.profile_dir_name = "Default"
        match sys.platform:
            case "win32":
                chrome.auth_path = Chrome.get_auth_path(ChromePaths.WINDOWS)
                chrome.application_path = Path(ChromePaths.WINDOWS_APP_PATH)
            case "darwin":
                chrome.auth_path = Chrome.get_auth_path(ChromePaths.MAC)
                chrome.application_path = Path(ChromePaths.MAC_APP_PATH)
            case "linux":
                chrome.auth_path = Chrome.get_auth_path(ChromePaths.LINUX)
                chrome.application_path = Path(ChromePaths.LINUX_APP_PATH)
            case _:
                raise ValueError(f"Unsupported platform {sys.platform}")
        return chrome

    @classmethod
    def manual_override_factory(
        cls,
        path: str | Path,
        application_path: str | Path,
        profile_dir_name: str,
    ) -> Self:
        """
        Use this to resolve multiple path conflicts
        """
        chrome = cls()
        chrome.auth_path = Path(path)
        chrome.application_path = Path(application_path)
        chrome.profile_dir_name = profile_dir_name
        return chrome

    @staticmethod
    def get_auth_path(indexedDB_path: str | Path) -> Path:
        """
        Parses all log files for a unexpired jwt token,
        doesn't validate the token against a network request
        """
        for f in Path(indexedDB_path).iterdir():
            if not f.suffix.endswith(".log"):
                continue
            raw_f = f.read_bytes().decode(errors="ignore")
            tokens = re.findall(r'\\"access_token\\",\\"([^\\"]+)', raw_f)
            for i in tokens:
                if decode_jwt(i)["exp"] > time.time():
                    return f
        raise RuntimeError("Unable to find a valid token path")

    def get_token(self, check_token: bool = False) -> str | None:
        """
        I"m like 99% sure that you can only have one valid auth token
        at a time per browser
        """
        raw_f = self.auth_path.read_bytes().decode(errors="ignore")
        tokens = re.findall(r'\\"access_token\\",\\"([^\\"]+)', raw_f)
        for t in tokens:
            if decode_jwt(t)["exp"] > time.time():
                if check_token and not is_token_valid(t):
                    raise RuntimeError("Token is not valid")
                return t
        if check_token:
            raise RuntimeError("Unable to find token from log file")
        else:
            return None

    def build_cmd_args(
        self,
        headless: bool,
        profile_name: str = "Default",
    ) -> list[str]:
        args = [str(self.application_path)]
        if headless:
            args.extend(["--headless=new", "--disable-gpu"])
        # No get is ok here if user doesn't have localapp data this
        # will fail regardless of a get
        app_data = os.environ["LOCALAPPDATA"]
        data_dir = Path(app_data) / "Goodle/Chrome/User Data"
        args.extend(
            [
                str(self.application_path),
                "--no-first-run",
                "--no-default-browser-check",
                f"--user-data-dir={data_dir}",
                f"--profile-directory={profile_name}",
            ]
        )
        return args

    @staticmethod
    def _is_open() -> bool:
        if sys.platform == "win32":
            check = subprocess.run(
                ["tasklist", "/fi", "imagename eq chrome.exe"],
                capture_output=True,
                text=True,
            )
            return "no tasks" in check.stdout.lower()

        else:
            check = subprocess.run(
                ["pgrep", "-fli", "chrome"],
                capture_output=True,
            )
            return check.returncode == 0

    def open_and_close_browser(
        self,
        retries: int = 3,
        close_after_seconds: float = 10,
        headless: bool = True,
    ) -> None:
        args = self.build_cmd_args(headless)
        logger.debug(f"Pre open time: {check_file_stat(self.auth_path):2f}")
        for _ in range(retries):
            try:
                proc = subprocess.Popen(
                    args, env=os.environ.copy(), **build_popen_kwargs()
                )
            except BlockingIOError:
                logger.debug("BlockingIOError retrying...")
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
            subprocess.run(["taskkill", "/IM", "chrome.exe"])
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                logger.debug("Timeout hit force killing process")
                subprocess.run(["taskkill", "/IM", "chrome.exe", "/T", "/F"])
                proc.wait()
        else:
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                logger.debug(
                    "Process %d already exited with %d",
                    proc.pid,
                    proc.returncode,
                )
                return None
            for s in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, s)
                except ProcessLookupError:
                    logger.debug("Process closed already")
                    try:
                        os.killpg(proc.pid, 0)
                        proc.wait()
                        return None
                    except ProcessLookupError:
                        continue
                try:
                    proc.wait(timeout)
                except subprocess.TimeoutExpired:
                    logger.debug("Signal %d failed", s)
            proc.wait()
