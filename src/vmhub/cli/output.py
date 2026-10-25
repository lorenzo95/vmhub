"""The little that paints the output: the Out class and its colours."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"


def colour(enabled: bool) -> bool:
    return enabled and sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


class Out:
    def __init__(self) -> None:
        self.c = colour(True)

    def paint(self, text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if self.c else text

    def dim(self, text: str) -> str:
        return self.paint(text, DIM)

    def bold(self, text: str) -> str:
        return self.paint(text, BOLD)

    def good(self, text: str) -> str:
        return self.paint(text, GREEN)

    def warn(self, text: str) -> str:
        return self.paint(text, YELLOW)

    def bad(self, text: str) -> str:
        return self.paint(text, RED)

    def info(self, text: str) -> str:
        return self.paint(text, CYAN)

    def say(self, message: str = "") -> None:
        print(message)

    def step(self, message: str) -> None:
        print(f"  {self.dim(message)}")


def confirm(prompt: str, *, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def open_url(url: str) -> int:
    launcher = shutil.which("xdg-open") or shutil.which("gio")
    if not launcher:
        print(f"Open this URL in your browser: {url}")
        return 1
    args = [launcher, "open", url] if launcher.endswith("gio") else [launcher, url]
    return subprocess.run(args).returncode
