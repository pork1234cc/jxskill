"""读取 TikHub 归一化作品详情中的共享字段。"""

from __future__ import annotations

from typing import Any


def feed_info(profile: dict[str, Any]) -> dict[str, Any]:
    """返回归一化作品信息字典。"""

    return (profile.get("data") or {}).get("feedInfo") or {}


def author_info(profile: dict[str, Any]) -> dict[str, Any]:
    """返回归一化作者信息字典。"""

    return (profile.get("data") or {}).get("authorInfo") or {}

