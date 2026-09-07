"""定义监控素材的结构化筛选条件。"""

from __future__ import annotations

import math
import operator
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from .tikhub import MonitorPost

DEFAULT_WINDOW_HOURS = 72
DEFAULT_MIN_FORWARD_COUNT = 500
DEFAULT_MIN_FORWARD_LIKE_RATIO = 1.5

METRIC_FIELDS = frozenset(
    {
        "like_count",
        "fav_count",
        "comment_count",
        "forward_count",
        "forward_like_ratio",
    }
)
COMPARATORS: dict[str, Callable[[float, float], bool]] = {
    "gte": operator.ge,
    "gt": operator.gt,
    "lte": operator.le,
    "lt": operator.lt,
    "eq": operator.eq,
}
FILTER_LOGICS = frozenset({"all", "any"})


@dataclass(frozen=True, slots=True)
class FilterCondition:
    """描述一个已经结构化并校验过的指标条件。"""

    field: str
    operator: str
    value: float

    def __post_init__(self) -> None:
        """拒绝未知字段、未知运算符和无效阈值。"""

        if self.field not in METRIC_FIELDS:
            raise ValueError(f"不支持的筛选字段：{self.field}")
        if self.operator not in COMPARATORS:
            raise ValueError(f"不支持的筛选运算符：{self.operator}")
        if not math.isfinite(self.value) or self.value < 0:
            raise ValueError("筛选阈值必须是大于或等于零的有限数字。")

    def matches(self, post: MonitorPost) -> bool:
        """判断作品指标是否满足当前条件，缺失值一律不通过。"""

        actual = metric_value(post, self.field)
        if actual is None:
            return False
        return COMPARATORS[self.operator](actual, self.value)

    def to_dict(self) -> dict[str, str | float]:
        """转换成可用于 CLI 回显的稳定字段。"""

        return {
            "field": self.field,
            "operator": self.operator,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class MonitorFilter:
    """组合时间窗口与多个指标条件。"""

    conditions: tuple[FilterCondition, ...] = ()
    logic: str = "all"
    window_hours: float = DEFAULT_WINDOW_HOURS

    def __post_init__(self) -> None:
        """校验组合逻辑与时间窗口。"""

        if self.logic not in FILTER_LOGICS:
            raise ValueError(f"不支持的筛选关系：{self.logic}")
        if not math.isfinite(self.window_hours) or self.window_hours <= 0:
            raise ValueError("监控时间窗口必须是大于零的有限小时数。")

    @property
    def window(self) -> timedelta:
        """返回可直接用于时间比较的监控窗口。"""

        return timedelta(hours=self.window_hours)

    def matches(self, post: MonitorPost) -> bool:
        """应用自定义条件；未提供条件时沿用原有默认门槛。"""

        if not self.conditions:
            return meets_default_threshold(post)
        results = [condition.matches(post) for condition in self.conditions]
        return all(results) if self.logic == "all" else any(results)

    def to_dict(self) -> dict[str, object]:
        """返回本轮实际使用的窗口、逻辑和指标条件。"""

        return {
            "window_hours": self.window_hours,
            "logic": self.logic,
            "uses_default_metrics": not self.conditions,
            "conditions": [condition.to_dict() for condition in self.conditions],
        }


def metric_value(post: MonitorPost, field: str) -> float | None:
    """读取单项指标，并安全计算转发点赞比。"""

    if field == "forward_like_ratio":
        if post.like_count is None or post.like_count <= 0:
            return None
        if post.forward_count is None:
            return None
        return post.forward_count / post.like_count
    value = getattr(post, field)
    return float(value) if value is not None else None


def meets_default_threshold(post: MonitorPost) -> bool:
    """保留无自定义条件时的原有转发数与转赞比规则。"""

    if post.forward_count is None or post.forward_count < DEFAULT_MIN_FORWARD_COUNT:
        return False
    if post.like_count is None:
        return False
    if post.like_count <= 0:
        return True
    return post.forward_count / post.like_count >= DEFAULT_MIN_FORWARD_LIKE_RATIO
