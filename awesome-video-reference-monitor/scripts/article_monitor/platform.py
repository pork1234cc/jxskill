"""识别对标作品链接所属平台。"""

from __future__ import annotations


def detect_platform(link: str) -> str:
    """识别抖音或微信视频号分享链接。"""

    text = str(link or "").lower()
    if (
        "v.douyin.com" in text
        or "douyin.com/video/" in text
        or "douyin.com/note/" in text
        or "douyin.com/discover" in text
        or "douyin.com/aweme/" in text
    ):
        return "douyin"
    if (
        "weixin.qq.com/sph/" in text
        or "channels.weixin.qq.com" in text
        or "finder.video.qq.com" in text
    ):
        return "wechat_channels"
    return "unknown"

