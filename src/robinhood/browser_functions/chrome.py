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
    profile_path: Path
    __slots__ = {
        "auth_path",
        "application_path",
        "profile_dir_name",
        "profile_path",
    }

    @classmethod
    def browser_factory(cls) -> Self:
        chrome = cls()
        chrome.profile_dir_name = "Default"
        match sys.platform:
            case "win32":
                chrome.auth_path = Chrome.get_auth_path(ChromePaths.WINDOWS)
                chrome.application_path = Path(ChromePaths.WINDOWS_APP_PATH)
                _app_data = Path(os.environ["LOCALAPPDATA"])
                chrome.profile_path = _app_data / "Google/Chrome/User Data"
            case "darwin":
                chrome.auth_path = Chrome.get_auth_path(ChromePaths.MAC)
                chrome.application_path = Path(ChromePaths.MAC_APP_PATH)
                chrome.profile_path = (
                    HOME_DIR / "Library/Application Support/Google/Chrome"
                )

            case "linux":
                chrome.auth_path = Chrome.get_auth_path(ChromePaths.LINUX)
                chrome.application_path = Path(ChromePaths.LINUX_APP_PATH)
                chrome.profile_path = HOME_DIR / ".config/google-chrome"
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
        raise RuntimeError(
            f"{Chrome.get_auth_path.__name__} failed to read an unexpired access token"  # noqa: E501
        )

    def get_token(self, check_token: bool = False) -> str | None:
        """
        I"m like 99% sure that you can only have one valid auth token
        at a time per browser
        """
        raw_f = self.auth_path.read_bytes().decode(errors="ignore")
        tokens: list[str] = re.findall(
            r'\\"access_token\\",\\"([^\\"]+)', raw_f
        )
        valid_tokens: list[str] = []
        for t in tokens:
            if decode_jwt(t)["exp"] > time.time():
                if check_token and not is_token_valid(t):
                    raise RuntimeError("Token is not valid")
                valid_tokens.append(t)
        if len(valid_tokens) > 1:
            logger.debug("Multiple valid token found resolving conflict")
            try:
                tokens = [i for i in valid_tokens if is_token_valid(i)]
                return tokens[0]
            except IndexError:
                if check_token:
                    raise RuntimeError("Unable to find token from log file")
                else:
                    return None
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
        args.extend(
            [
                "--no-first-run",
                "--no-default-browser-check",
                f"--user-data-dir={self.profile_path}",
                f"--profile-directory={profile_name}",
                "https://robinhood.com",
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
                break
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
        if proc.poll() is not None:
            logger.debug(
                "Process %d already exited with %s",
                proc.pid,
                proc.returncode,
            )
            return
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T"],
                check=False,
            )
            try:
                proc.wait(timeout)
                return None
            except subprocess.TimeoutExpired:
                logger.debug(
                    "SIGTERM equivalent timed out, force killing process"
                )
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                check=False,
            )
            proc.wait()
            return None
        # macOS / Linux
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            proc.poll()
            logger.debug(
                "Process group %d already exited",
                proc.pid,
            )
            return None
        try:
            proc.wait(timeout)
            return None
        except subprocess.TimeoutExpired:
            logger.debug(
                "SIGTERM failed for process group %d, sending SIGKILL",
                proc.pid,
            )
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
