"""提供项目内多维表格模块共用的飞书鉴权与基础读写能力。"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HttpTransport = Callable[[str, str, Mapping[str, str], bytes | None], dict[str, Any]]


class FeishuBitableError(RuntimeError):
    """表示飞书多维表格鉴权、读取或写入失败。"""


@dataclass(frozen=True, slots=True)
class FeishuTableConfig:
    """描述一张多维表格所需的应用和数据表标识。"""

    app_id: str
    app_secret: str
    app_token: str
    table_id: str


def read_env_file(path: Path) -> dict[str, str]:
    """读取 UTF-8 dotenv，兼容包含空格的历史配置键。"""
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def first_value(values: Mapping[str, str], *keys: str) -> str:
    """按顺序读取第一个非空配置值。"""
    folded = {str(key).casefold(): str(value).strip() for key, value in values.items()}
    for key in keys:
        value = folded.get(key.casefold(), "")
        if value:
            return value
    return ""


def app_credentials(values: Mapping[str, str]) -> tuple[str, str, str]:
    """从新旧配置键中读取应用 ID、密钥和多维表格应用令牌。"""
    return (
        first_value(values, "FEISHU_APP_ID", "APPID"),
        first_value(values, "FEISHU_APP_SECRET", "APPSECRET", "App Secret"),
        first_value(
            values,
            "FEISHU_APP_TOKEN",
            "FEISHU_REMIX_APP_TOKEN",
            "APP_TOKEN",
        ),
    )


def urlopen_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
) -> dict[str, Any]:
    """使用标准库发送一次飞书 HTTP 请求。"""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        response_text = exc.read().decode("utf-8", errors="replace")[:500]
        raise FeishuBitableError(
            f"飞书 HTTP 请求失败：status={exc.code}, body={response_text}"
        ) from exc
    except urllib.error.URLError as exc:
        raise FeishuBitableError(f"飞书网络请求失败：{exc.reason}") from exc
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise FeishuBitableError("飞书返回了无效 JSON。") from exc


class FeishuBitableClient:
    """以应用身份访问指定飞书多维数据表。"""

    def __init__(
        self,
        config: FeishuTableConfig,
        transport: HttpTransport = urlopen_transport,
    ) -> None:
        if not all((config.app_id, config.app_secret, config.app_token, config.table_id)):
            raise FeishuBitableError("飞书多维表格配置不完整。")
        self.config = config
        self._transport = transport
        self._tenant_token = ""

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        """请求飞书接口并统一检查业务错误码。"""
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._access_token()}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
        response = self._transport(method, f"https://open.feishu.cn{path}", headers, body)
        if int(response.get("code", -1)) != 0:
            raise FeishuBitableError(
                f"飞书 API 请求失败：code={response.get('code')}, msg={response.get('msg')}"
            )
        return response

    def _access_token(self) -> str:
        """获取并缓存本次进程使用的 tenant_access_token。"""
        if self._tenant_token:
            return self._tenant_token
        response = self._request(
            "POST",
            "/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": self.config.app_id, "app_secret": self.config.app_secret},
            authenticated=False,
        )
        token = str(response.get("tenant_access_token") or "")
        if not token:
            raise FeishuBitableError("飞书鉴权成功但未返回 tenant_access_token。")
        self._tenant_token = token
        return token

    def list_records(self) -> list[dict[str, Any]]:
        """分页读取目标表记录。"""
        return self._list_items("records", page_size=500, label="记录")

    def list_fields(self) -> list[dict[str, Any]]:
        """分页读取目标表字段定义。"""
        return self._list_items("fields", page_size=100, label="字段")

    def _list_items(self, resource: str, *, page_size: int, label: str) -> list[dict[str, Any]]:
        """分页读取目标表下的一类资源。"""
        items: list[dict[str, Any]] = []
        page_token = ""
        while True:
            query = {"page_size": str(page_size)}
            if page_token:
                query["page_token"] = page_token
            path = f"{self._table_path()}/{resource}?{urllib.parse.urlencode(query)}"
            data = self._request("GET", path).get("data") or {}
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                return items
            page_token = str(data.get("page_token") or "")
            if not page_token:
                raise FeishuBitableError(f"飞书{label}分页缺少 page_token。")

    def upsert(self, fields: dict[str, Any], record_id: str = "") -> str:
        """新增或更新一条记录并返回记录 ID。"""
        payload = {"fields": fields}
        if record_id:
            response = self._request("PUT", f"{self._table_path()}/records/{record_id}", payload)
        else:
            response = self._request("POST", f"{self._table_path()}/records", payload)
        saved = ((response.get("data") or {}).get("record") or {}).get("record_id")
        if not saved:
            raise FeishuBitableError("飞书写入成功但未返回 record_id。")
        return str(saved)

    def create_field(
        self,
        field_name: str,
        field_type: int,
        property_: dict[str, Any] | None,
    ) -> str:
        """创建字段并返回字段 ID。"""
        payload: dict[str, Any] = {"field_name": field_name, "type": field_type}
        if property_ is not None:
            payload["property"] = property_
        response = self._request("POST", f"{self._table_path()}/fields", payload)
        field = (response.get("data") or {}).get("field") or {}
        field_id = str(field.get("field_id") or "")
        if not field_id:
            raise FeishuBitableError("飞书创建字段成功但未返回 field_id。")
        return field_id

    def _table_path(self) -> str:
        """返回当前目标表的 API 路径。"""
        return (
            f"/open-apis/bitable/v1/apps/{self.config.app_token}"
            f"/tables/{self.config.table_id}"
        )


def plain_text(value: Any) -> str:
    """兼容飞书文本字段的字符串和富文本片段返回值。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [str(item.get("text") or "") for item in value if isinstance(item, dict)]
        return "".join(parts).strip()
    return str(value or "").strip()
