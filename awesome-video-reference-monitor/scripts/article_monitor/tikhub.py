"""TikHub 双平台账号与作品列表客户端。"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import MonitorConfig, require_tikhub_config
from .platform import detect_platform

DOUYIN_DETAIL_PATH = "/api/v1/douyin/app/v3/fetch_one_video_by_share_url"
DOUYIN_POSTS_PATH = "/api/v1/douyin/app/v3/fetch_user_post_videos"
WECHAT_DETAIL_PATH = "/api/v1/wechat_channels/v2/fetch_video_detail"
WECHAT_POSTS_PATH = "/api/v1/wechat_channels/v2/fetch_user_videos"
DEFAULT_TIMEOUT_SECONDS = 45


@dataclass(frozen=True, slots=True)
class MonitorAccount:
    """描述一个可稳定查询的平台账号。"""

    platform: str
    nickname: str
    username: str
    api_user_id: str


@dataclass(frozen=True, slots=True)
class MonitorPost:
    """描述已丢弃播放数后的统一视频作品。"""

    platform: str
    video_id: str
    object_nonce_id: str
    nickname: str
    username: str
    title: str
    create_time: int
    like_count: int | None
    fav_count: int | None
    forward_count: int | None
    comment_count: int | None
    media_url: str
    decode_key: str = ""
    is_video: bool = True

    @property
    def case_id(self) -> str:
        """返回跨轮次稳定的本地案例 ID。"""

        return f"{self.platform}_{self.video_id}"

    @property
    def source_url(self) -> str:
        """返回可用的平台作品地址；视频号暂不伪造公开分享链接。"""

        if self.platform == "douyin":
            return f"https://www.douyin.com/video/{self.video_id}"
        return ""

    def to_profile(self) -> dict[str, Any]:
        """转换为共享采集核心接受的归一化详情结构。"""

        profile: dict[str, Any] = {
            "platform": self.platform,
            "data": {
                "authorInfo": {
                    "nickname": self.nickname,
                    "username": self.username,
                },
                "feedInfo": {
                    "objectId": self.video_id,
                    "objectNonceId": self.object_nonce_id,
                    "title": self.title,
                    "description": self.title,
                    "createTime": self.create_time,
                    "likeCount": self.like_count,
                    "favCount": self.fav_count,
                    "forwardCount": self.forward_count,
                    "commentCount": self.comment_count,
                    "videoUrl": self.media_url,
                    "h264VideoInfo": {"videoUrl": self.media_url},
                },
            },
        }
        if self.platform == "wechat_channels":
            profile["_wechatDecrypt"] = {
                "decodeKey": self.decode_key,
                "scheme": "isaac64-xor-first-128k",
            }
        return profile


@dataclass(frozen=True, slots=True)
class PostPage:
    """描述一页已归一化作品及其下一页游标。"""

    posts: tuple[MonitorPost, ...]
    has_more: bool
    cursor: str


class TikHubClient:
    """使用标准库 HTTP 调用 TikHub，并集中校验响应信封。"""

    def __init__(self, config: MonitorConfig) -> None:
        """保存已校验的 TikHub 配置。"""

        require_tikhub_config(config)
        self.api_key = config.tikhub_api_key
        self.base_url = config.tikhub_base_url.rstrip("/")

    def fetch_account(self, share_url: str) -> MonitorAccount:
        """通过任意一条作品分享链接解析稳定账号标识。"""

        platform = detect_platform(share_url)
        if platform == "douyin":
            payload = self._request("GET", DOUYIN_DETAIL_PATH, query={"share_url": share_url})
            return normalize_douyin_account(response_data(payload))
        if platform == "wechat_channels":
            payload = self._request(
                "POST",
                WECHAT_DETAIL_PATH,
                body={"share_url": share_url, "raw": False},
            )
            return normalize_wechat_account(response_data(payload))
        raise RuntimeError("暂不支持该链接平台，请输入抖音或视频号公开视频链接。")

    def fetch_post(self, share_url: str) -> MonitorPost:
        """通过作品分享链接读取单条视频详情并归一化。

        参数：
            share_url: 抖音或微信视频号的公开作品链接。
        返回：
            包含媒体地址、作品标识和作者信息的统一作品对象。
        异常：
            RuntimeError: 链接平台不支持，或详情缺少稳定作品 ID。
        """

        platform = detect_platform(share_url)
        if platform == "douyin":
            payload = self._request(
                "GET",
                DOUYIN_DETAIL_PATH,
                query={"share_url": share_url},
            )
            data = response_data(payload)
            item = _first_dict(data, "aweme_detail", "awemeDetail", "item") or data
            account = MonitorAccount("douyin", "", "", "")
            post = normalize_douyin_post(item, account)
        elif platform == "wechat_channels":
            payload = self._request(
                "POST",
                WECHAT_DETAIL_PATH,
                body={"share_url": share_url, "raw": False},
            )
            data = response_data(payload)
            username = _text(data.get("username"))
            account = MonitorAccount(
                "wechat_channels",
                _text(data.get("nickname")),
                username,
                username,
            )
            post = normalize_wechat_post(data, account)
        else:
            raise RuntimeError(
                "暂不支持该链接平台，请输入抖音或视频号公开视频链接。"
            )
        if not post.video_id:
            raise RuntimeError("TikHub 作品详情缺少稳定作品 ID。")
        return post

    def fetch_posts(
        self,
        account: MonitorAccount,
        *,
        cursor: str = "",
        channel: str = "normal",
    ) -> PostPage:
        """读取指定账号的一页作品并归一化为统一结构。"""

        if account.platform == "douyin":
            payload = self._request(
                "GET",
                DOUYIN_POSTS_PATH,
                query={
                    "sec_user_id": account.api_user_id,
                    "max_cursor": cursor or "0",
                    "count": 20,
                    "sort_type": 0,
                    "channel": channel,
                },
            )
            return normalize_douyin_page(response_data(payload), account)
        if account.platform == "wechat_channels":
            payload = self._request(
                "POST",
                WECHAT_POSTS_PATH,
                body={"username": account.api_user_id, "last_buffer": cursor, "raw": False},
            )
            return normalize_wechat_page(response_data(payload), account)
        raise RuntimeError(f"不支持的账号平台：{account.platform}")

    def fetch_douyin_post(
        self,
        video_id: str,
        account: MonitorAccount,
    ) -> MonitorPost:
        """列表缺少播放地址时，通过稳定作品页补查抖音详情。"""

        share_url = f"https://www.douyin.com/video/{video_id}"
        payload = self._request(
            "GET",
            DOUYIN_DETAIL_PATH,
            query={"share_url": share_url},
        )
        data = response_data(payload)
        aweme = _first_dict(data, "aweme_detail", "awemeDetail", "item") or data
        return normalize_douyin_post(aweme, account)

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发送带 Bearer Token 的请求并返回 JSON 对象。"""

        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "video-reference-monitor/1.0",
            },
        )
        return open_json_request(request)


def open_json_request(
    request: urllib.request.Request,
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """执行 TikHub 请求，并把网络与 JSON 错误转换为可读异常。"""

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"TikHub HTTP {exc.code}: {detail[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"TikHub 网络错误: {exc.reason}") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"TikHub 返回非 JSON: {text[:500]}") from exc
    if not isinstance(payload, dict):
        raise TypeError("TikHub 返回的 JSON 顶层不是对象。")
    return payload


def response_data(payload: dict[str, Any]) -> dict[str, Any]:
    """校验 TikHub 通用响应码并提取 data 对象。"""

    code = payload.get("code")
    if code not in (None, 0, 200, "200"):
        message = payload.get("message") or payload.get("detail") or payload.get("error")
        raise RuntimeError(f"TikHub 接口返回失败（{code}）：{message or '未知错误'}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise TypeError("TikHub 响应缺少 data 对象。")
    return data


def normalize_douyin_account(data: dict[str, Any]) -> MonitorAccount:
    """从抖音单作品详情提取昵称、抖音号和 sec_user_id。"""

    aweme = _first_dict(data, "aweme_detail", "awemeDetail", "item") or data
    author = _first_dict(aweme, "author", "author_info", "authorInfo")
    api_user_id = _text(author.get("sec_uid") or author.get("sec_user_id"))
    if not api_user_id:
        raise RuntimeError("TikHub 抖音详情缺少作者 sec_user_id。")
    return MonitorAccount(
        platform="douyin",
        nickname=_text(author.get("nickname")),
        username=_text(author.get("unique_id")),
        api_user_id=api_user_id,
    )


def normalize_wechat_account(data: dict[str, Any]) -> MonitorAccount:
    """从视频号精简详情提取 finder username。"""

    username = _text(data.get("username"))
    if not username:
        raise RuntimeError("TikHub 视频号详情缺少 finder username。")
    return MonitorAccount(
        platform="wechat_channels",
        nickname=_text(data.get("nickname")),
        username=username,
        api_user_id=username,
    )


def normalize_douyin_page(data: dict[str, Any], account: MonitorAccount) -> PostPage:
    """归一化抖音作品页，并优先选择 H.264 播放地址。"""

    raw_posts = data.get("aweme_list") or data.get("awemeList") or []
    posts = tuple(
        normalize_douyin_post(item, account)
        for item in raw_posts
        if isinstance(item, dict)
    )
    return PostPage(
        posts=posts,
        has_more=bool(data.get("has_more") or data.get("hasMore")),
        cursor=_text(data.get("max_cursor") or data.get("maxCursor")),
    )


def normalize_douyin_post(item: dict[str, Any], account: MonitorAccount) -> MonitorPost:
    """归一化单条抖音作品，不读取播放数。"""

    author = _first_dict(item, "author", "author_info", "authorInfo")
    statistics = _first_dict(item, "statistics", "stats")
    video = _first_dict(item, "video")
    media_url = _douyin_video_url(video)
    return MonitorPost(
        platform="douyin",
        video_id=_text(item.get("aweme_id") or item.get("awemeId")),
        object_nonce_id="",
        nickname=_text(author.get("nickname")) or account.nickname,
        username=_text(author.get("unique_id")) or account.username,
        title=_text(item.get("desc") or item.get("title")),
        create_time=_integer(item.get("create_time") or item.get("createTime")),
        like_count=_optional_integer(_first_present(statistics, "digg_count", "like_count")),
        fav_count=_optional_integer(_first_present(statistics, "collect_count", "fav_count")),
        forward_count=_optional_integer(
            _first_present(statistics, "share_count", "forward_count")
        ),
        comment_count=_optional_integer(statistics.get("comment_count")),
        media_url=media_url,
        is_video=bool(video),
    )


def normalize_wechat_page(data: dict[str, Any], account: MonitorAccount) -> PostPage:
    """归一化视频号作品页，保持媒体地址与解密密钥成对。"""

    raw_posts = data.get("videos") or []
    posts = tuple(
        normalize_wechat_post(item, account)
        for item in raw_posts
        if isinstance(item, dict)
    )
    return PostPage(
        posts=posts,
        has_more=bool(data.get("up_continue")),
        cursor=_text(data.get("last_buffer")),
    )


def normalize_wechat_post(item: dict[str, Any], account: MonitorAccount) -> MonitorPost:
    """归一化单条视频号作品并显式忽略 read_count。"""

    media = _first_dict(item, "media")
    media_url = _text(media.get("full_url")) or (
        f"{_text(media.get('url'))}{_text(media.get('url_token'))}"
    )
    return MonitorPost(
        platform="wechat_channels",
        video_id=_text(item.get("id")),
        object_nonce_id=_text(item.get("object_nonce_id")),
        nickname=_text(item.get("nickname")) or account.nickname,
        username=_text(item.get("username")) or account.username,
        title=_title_text(item.get("title")) or _title_text(item.get("description")),
        create_time=_integer(item.get("create_time")),
        like_count=_optional_integer(item.get("like_count")),
        fav_count=_optional_integer(item.get("fav_count")),
        forward_count=_optional_integer(item.get("forward_count")),
        comment_count=_optional_integer(item.get("comment_count")),
        media_url=media_url,
        decode_key=_text(media.get("decode_key")),
        is_video=bool(media),
    )


def _douyin_video_url(video: dict[str, Any]) -> str:
    """按 H.264、通用播放地址、码率列表顺序选择抖音媒体 URL。"""

    for key in ("play_addr_h264", "play_addr", "play_addr_265"):
        url = _url_from_address(video.get(key))
        if url:
            return url
    for item in video.get("bit_rate") or []:
        if isinstance(item, dict):
            url = _url_from_address(item.get("play_addr"))
            if url:
                return url
    return ""


def _url_from_address(value: Any) -> str:
    """从 TikHub 播放地址对象或字符串中取首个 URL。"""

    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    urls = value.get("url_list") or value.get("urlList") or []
    if isinstance(urls, list):
        for item in urls:
            if _text(item):
                return _text(item)
    return _text(value.get("url"))


def _first_dict(value: dict[str, Any], *keys: str) -> dict[str, Any]:
    """返回多个候选字段中的第一个字典值。"""

    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, dict):
            return candidate
    return {}


def _first_present(value: dict[str, Any], *keys: str) -> Any:
    """返回多个候选字段中的第一个非空值，并保留数字零。"""

    for key in keys:
        candidate = value.get(key)
        if candidate not in (None, ""):
            return candidate
    return None


def _title_text(value: Any) -> str:
    """从字符串、字典或列表标题结构中提取首个有效纯文本。"""

    if isinstance(value, dict):
        for key in ("shortTitle", "title", "description", "desc", "text", "content"):
            text = _title_text(value.get(key))
            if text:
                return text
        return ""
    if isinstance(value, list):
        for item in value:
            text = _title_text(item)
            if text:
                return text
        return ""
    return _text(value)


def _text(value: Any) -> str:
    """把平台标识无损转换为去空白字符串。"""

    return str(value if value is not None else "").strip()


def _integer(value: Any) -> int:
    """把时间戳转换为整数，缺失或异常时返回零。"""

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _optional_integer(value: Any) -> int | None:
    """把可选互动数字转换为整数，缺失或异常时返回空。"""

    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
