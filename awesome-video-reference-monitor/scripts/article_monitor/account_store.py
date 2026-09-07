"""把对标账号幂等登记到飞书，并为监控提供只读账号列表。"""
from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from .feishu import (
    FeishuBitableClient,
    FeishuBitableError,
    FeishuTableConfig,
    app_credentials,
    first_value,
    plain_text,
)
from .tikhub import MonitorAccount

CHINA_TIMEZONE = timezone(timedelta(hours=8))
PLATFORM_LABELS = {"douyin": "抖音", "wechat_channels": "视频号"}
PLATFORM_KEYS = {value: key for key, value in PLATFORM_LABELS.items()}
ACCOUNT_FIELD_TYPES = {
    "序号": 1,
    "作者": 1,
    "平台": 3,
    "账号标识": 1,
    "API查询ID": 1,
    "登记作品链接": 15,
    "记录时间": 5,
}
CREATABLE_FIELD_PROPERTIES: dict[str, dict[str, Any] | None] = {
    "作者": {},
    "平台": {"options": [{"name": "视频号"}, {"name": "抖音"}]},
    "账号标识": {},
    "API查询ID": {},
    "登记作品链接": None,
    "记录时间": {"date_formatter": "yyyy-MM-dd HH:mm"},
}


class AccountBitableError(RuntimeError):
    """表示飞书对标账号表结构或记录不符合约定。"""


class MonitorAccountBitableClient(FeishuBitableClient):
    """自动补齐账号字段，并按平台和API查询ID幂等读写账号。"""

    def ensure_schema(self) -> None:
        """创建缺失的非主字段，并在创建后重新校验完整结构。"""

        indexed = self._indexed_fields()
        self._raise_wrong_types(indexed)
        if "序号" not in indexed:
            raise AccountBitableError("飞书对标账号表缺少主字段“序号”。")
        for field_name, field_type in ACCOUNT_FIELD_TYPES.items():
            if field_name in indexed:
                continue
            try:
                self.create_field(
                    field_name,
                    field_type,
                    CREATABLE_FIELD_PROPERTIES[field_name],
                )
            except FeishuBitableError as exc:
                raise AccountBitableError(str(exc)) from exc
        self.validate_schema()

    def validate_schema(self) -> None:
        """只读校验账号表字段，监控命令不得借此创建或修改字段。"""

        indexed = self._indexed_fields()
        missing = [name for name in ACCOUNT_FIELD_TYPES if name not in indexed]
        if missing:
            raise AccountBitableError(
                f"飞书对标账号表缺少字段：{'、'.join(missing)}；请先执行“记录对标”。"
            )
        self._raise_wrong_types(indexed)
        platform_options = {
            str(item.get("name") or "")
            for item in ((indexed["平台"].get("property") or {}).get("options") or [])
            if isinstance(item, dict)
        }
        required_options = set(PLATFORM_LABELS.values())
        if not required_options.issubset(platform_options):
            missing_options = "、".join(sorted(required_options - platform_options))
            raise AccountBitableError(f"飞书字段“平台”缺少选项：{missing_options}")

    def sync_account(
        self,
        account: MonitorAccount,
        share_url: str,
        recorded_at: datetime,
    ) -> dict[str, str]:
        """新增账号，或更新平台与API查询ID相同的唯一记录。"""

        if account.platform not in PLATFORM_LABELS:
            raise AccountBitableError(f"不支持的账号平台：{account.platform}")
        if not account.api_user_id.strip():
            raise AccountBitableError("API查询ID不能为空。")
        self.ensure_schema()
        records = self.list_records()
        record_id = self._resolve_record_id(records, account)
        fields = self._account_fields(account, share_url)
        if not record_id:
            fields["序号"] = str(len(records) + 1).zfill(4)
            fields["记录时间"] = _timestamp_milliseconds(recorded_at)
        try:
            saved_id = self.upsert(fields, record_id)
        except FeishuBitableError as exc:
            raise AccountBitableError(str(exc)) from exc
        return {
            "record_id": saved_id,
            "action": "update" if record_id else "create",
            "account_id": f"{account.platform}:{account.api_user_id}",
            "platform": account.platform,
            "api_user_id": account.api_user_id,
        }

    def list_monitor_accounts(self) -> list[MonitorAccount]:
        """只读返回具备平台和API查询ID的有效账号，并拒绝重复稳定键。"""

        self.validate_schema()
        accounts: list[MonitorAccount] = []
        seen: set[tuple[str, str]] = set()
        for item in self.list_records():
            fields = item.get("fields") or {}
            platform = PLATFORM_KEYS.get(plain_text(fields.get("平台")), "")
            api_user_id = plain_text(fields.get("API查询ID"))
            if not platform or not api_user_id:
                continue
            key = (platform, api_user_id)
            if key in seen:
                raise AccountBitableError(
                    f"飞书对标账号表存在重复账号：{platform} / {api_user_id}"
                )
            seen.add(key)
            accounts.append(
                MonitorAccount(
                    platform=platform,
                    nickname=plain_text(fields.get("作者")),
                    username=plain_text(fields.get("账号标识")),
                    api_user_id=api_user_id,
                )
            )
        return accounts

    def _indexed_fields(self) -> dict[str, dict[str, Any]]:
        """按字段名索引当前飞书字段定义。"""

        try:
            fields = self.list_fields()
        except FeishuBitableError as exc:
            raise AccountBitableError(str(exc)) from exc
        return {
            str(field.get("field_name") or ""): field
            for field in fields
            if isinstance(field, dict)
        }

    @staticmethod
    def _raise_wrong_types(indexed: Mapping[str, dict[str, Any]]) -> None:
        """发现同名字段类型错误时停止，避免创建重名或破坏数据。"""

        wrong_types = [
            f"{name}(应为{field_type}，实际为{indexed[name].get('type')})"
            for name, field_type in ACCOUNT_FIELD_TYPES.items()
            if name in indexed and indexed[name].get("type") != field_type
        ]
        if wrong_types:
            raise AccountBitableError(
                f"飞书对标账号表字段类型不匹配：{'、'.join(wrong_types)}"
            )

    @staticmethod
    def _resolve_record_id(
        records: list[dict[str, Any]],
        account: MonitorAccount,
    ) -> str:
        """优先按稳定ID匹配，并兼容尚未补充新字段的旧账号行。"""

        platform_label = PLATFORM_LABELS[account.platform]
        stable_matches: list[str] = []
        legacy_matches: list[str] = []
        for item in records:
            fields = item.get("fields") or {}
            if plain_text(fields.get("平台")) != platform_label:
                continue
            record_id = str(item.get("record_id") or "")
            if not record_id:
                continue
            existing_api_id = plain_text(fields.get("API查询ID"))
            if existing_api_id == account.api_user_id:
                stable_matches.append(record_id)
            elif not existing_api_id and plain_text(fields.get("作者")) == account.nickname:
                legacy_matches.append(record_id)
        matches = stable_matches or legacy_matches
        if len(matches) > 1:
            identity = account.api_user_id if stable_matches else account.nickname
            raise AccountBitableError(
                f"飞书对标账号表存在多个匹配记录：{platform_label} / {identity}"
            )
        return matches[0] if matches else ""

    @staticmethod
    def _account_fields(account: MonitorAccount, share_url: str) -> dict[str, Any]:
        """把统一账号模型转换成飞书账号字段。"""

        url = share_url.strip()
        return {
            "作者": account.nickname.strip(),
            "平台": PLATFORM_LABELS[account.platform],
            "账号标识": account.username.strip(),
            "API查询ID": account.api_user_id.strip(),
            "登记作品链接": {"text": url, "link": url},
        }


def account_bitable_client_from_env(
    values: Mapping[str, str] | None = None,
) -> MonitorAccountBitableClient:
    """使用环境中的飞书凭据和DUIBIAO_TABLE_ID创建账号表客户端。"""

    source = dict(os.environ) if values is None else values
    app_id, app_secret, app_token = app_credentials(source)
    table_id = first_value(source, "DUIBIAO_TABLE_ID")
    if not all((app_id, app_secret, app_token, table_id)):
        raise AccountBitableError(
            "飞书账号表配置不完整，需要应用凭据、APP_TOKEN 和 DUIBIAO_TABLE_ID。"
        )
    try:
        return MonitorAccountBitableClient(
            FeishuTableConfig(app_id, app_secret, app_token, table_id)
        )
    except FeishuBitableError as exc:
        raise AccountBitableError(str(exc)) from exc


def _timestamp_milliseconds(value: datetime) -> int:
    """把记录时间转换成飞书日期字段使用的毫秒时间戳。"""

    aware = value.replace(tzinfo=CHINA_TIMEZONE) if value.tzinfo is None else value
    return int(aware.astimezone(CHINA_TIMEZONE).timestamp() * 1000)
