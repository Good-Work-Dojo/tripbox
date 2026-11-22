# SPDX-License-Identifier: AGPL-3.0-only
# Additional terms: see NOTICE
"""The page's addresses, every one built from TRIPBOX_BASE_URL and none from a request: a path
under the base for a link or a redirect, the whole address for a calendar feed or a message."""

from __future__ import annotations

from dataclasses import dataclass

from tripbox.config import Settings

# The longest address `inside` takes.
MAX_TARGET = 2000


@dataclass(frozen=True)
class Urls:
    origin: str  # scheme://host[:port]
    base: str  # the path the page is served under: "" or such as /a/b

    @classmethod
    def of(cls, settings: Settings) -> Urls:
        return cls(settings.origin, settings.base_path)

    def path(self, p: str) -> str:
        """What a link or a redirect gives for `p`, a path written from `/`: a path, never an
        absolute address."""
        return self.base + p

    def absolute(self, p: str) -> str:
        """The whole address of `p`, for what is opened from outside the page."""
        return self.origin + self.base + p

    @property
    def cookie_path(self) -> str:
        return self.base or "/"

    @property
    def secure(self) -> bool:
        """The page is opened over https."""
        return self.origin.startswith("https://")

    def inside(self, target: str) -> bool:
        """Whether `target` is a path-only reference under the base: it begins `base + "/"`,
        holds no `//`, backslash or control character, no `.` or `..` segment (spelled out or
        encoded, which a browser resolves away) and at most MAX_TARGET characters."""
        if len(target) > MAX_TARGET or not target.startswith(self.base + "/"):
            return False
        if "//" in target or "\\" in target:
            return False
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in target):
            return False
        path = target.split("?", 1)[0].split("#", 1)[0]
        return all(
            seg.lower().replace("%2e", ".") not in (".", "..") for seg in path.split("/")
        )
