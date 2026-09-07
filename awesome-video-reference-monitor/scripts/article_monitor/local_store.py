"""使用项目内 Markdown 表格保存对标账号清单。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .tikhub import MonitorAccount

STORE_MARKER = "<!-- articlemonitor-account-store:v1 -->"
SUPPORTED_PLATFORMS = {"douyin", "wechat_channels"}
PLATFORM_LABELS = {"douyin": "抖音", "wechat_channels": "微信视频号"}
PLATFORM_KEYS = {value: key for key, value in PLATFORM_LABELS.items()}
REQUIRED_ACCOUNT_FIELDS = ("platform", "nickname", "username", "api_user_id")
TABLE_HEADERS = (
    "账号 ID",
    "平台",
    "作者",
    "账号标识",
    "API 查询 ID",
    "登记作品链接",
    "记录时间",
)


class LocalAccountStoreError(RuntimeError):
    """表示本地账号清单不存在可安全继续处理的结构。"""


class LocalAccountStore:
    """按“平台 + API 查询 ID”幂等读写本地账号清单。"""

    def __init__(self, path: Path | str) -> None:
        """保存账号清单路径，文件在首次登记时创建。"""

        self.path = Path(path)

    def sync_account(
        self,
        account: MonitorAccount,
        share_url: str,
        recorded_at: datetime,
    ) -> dict[str, str]:
        """新增账号，或更新平台与 API 查询 ID 相同的现有账号。"""

        self._validate_account(account)
        accounts = self._read_accounts()
        account_id = self._account_id(account.platform, account.api_user_id)
        matching_indexes = [
            index
            for index, item in enumerate(accounts)
            if self._account_id(item["platform"], item["api_user_id"]) == account_id
        ]
        if len(matching_indexes) > 1:
            raise LocalAccountStoreError(f"本地账号清单存在重复账号：{account_id}")

        record = {
            "account_id": account_id,
            "platform": account.platform,
            "nickname": account.nickname.strip(),
            "username": account.username.strip(),
            "api_user_id": account.api_user_id.strip(),
            "share_url": share_url.strip(),
        }
        if matching_indexes:
            index = matching_indexes[0]
            record["recorded_at"] = accounts[index].get("recorded_at", "")
            accounts[index] = record
            action = "update"
        else:
            record["recorded_at"] = recorded_at.isoformat()
            accounts.append(record)
            action = "create"

        self._write_accounts(accounts)
        return {"account_id": account_id, "action": action}

    def list_monitor_accounts(self) -> list[MonitorAccount]:
        """按文件顺序返回全部可监控账号，并拒绝重复稳定键。"""

        accounts = self._read_accounts()
        result: list[MonitorAccount] = []
        seen: set[str] = set()
        for item in accounts:
            account_id = self._account_id(item["platform"], item["api_user_id"])
            if account_id in seen:
                raise LocalAccountStoreError(f"本地账号清单存在重复账号：{account_id}")
            seen.add(account_id)
            result.append(
                MonitorAccount(
                    platform=item["platform"],
                    nickname=item["nickname"],
                    username=item["username"],
                    api_user_id=item["api_user_id"],
                )
            )
        return result

    def _read_accounts(self) -> list[dict[str, str]]:
        """读取并校验账号清单，文件不存在时返回空列表。"""

        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError) as exc:
            raise LocalAccountStoreError(f"无法读取本地账号清单：{self.path}") from exc

        if STORE_MARKER not in lines:
            self._raise_invalid_markdown("缺少格式版本标记")

        header_index = -1
        for index, line in enumerate(lines):
            if _split_table_row(line) == list(TABLE_HEADERS):
                header_index = index
                break
        if header_index < 0 or header_index + 1 >= len(lines):
            self._raise_invalid_markdown("缺少账号表头")
        separator = _split_table_row(lines[header_index + 1])
        if len(separator) != len(TABLE_HEADERS) or not all(
            len(cell.strip(":-")) == 0 and cell.count("-") >= 3
            for cell in separator
        ):
            self._raise_invalid_markdown("账号表格分隔行无效")

        accounts: list[dict[str, str]] = []
        for line_number, line in enumerate(lines[header_index + 2 :], start=header_index + 3):
            if not line.strip():
                continue
            values = _split_table_row(line)
            if len(values) != len(TABLE_HEADERS):
                self._raise_invalid_markdown(f"第 {line_number} 行列数无效")
            account_id, platform_label, nickname, username, api_user_id, share_url, recorded_at = values
            platform = PLATFORM_KEYS.get(platform_label, "")
            normalized = {
                "account_id": account_id,
                "platform": platform,
                "nickname": nickname,
                "username": username,
                "api_user_id": api_user_id,
                "share_url": share_url,
                "recorded_at": recorded_at,
            }
            missing = [key for key in REQUIRED_ACCOUNT_FIELDS if not normalized.get(key)]
            if missing:
                raise LocalAccountStoreError(
                    f"本地账号清单第 {line_number} 行缺少字段：{'、'.join(missing)}"
                )
            if not platform:
                raise LocalAccountStoreError(
                    f"本地账号清单第 {line_number} 行平台不受支持：{platform_label}"
                )
            expected_id = self._account_id(platform, api_user_id)
            if account_id != expected_id:
                raise LocalAccountStoreError(
                    f"本地账号清单第 {line_number} 行账号 ID 应为：{expected_id}"
                )
            accounts.append(normalized)
        return accounts

    def _write_accounts(self, accounts: list[dict[str, str]]) -> None:
        """原子写入 UTF-8 Markdown，避免中断时破坏已有账号清单。"""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_name(f"{self.path.name}.tmp")
        lines = [
            "# 对标账号",
            "",
            STORE_MARKER,
            "",
            "> 由 articlemonitor 维护；可手动删除账号整行，请勿修改表头或账号 ID。",
            "",
            "| " + " | ".join(TABLE_HEADERS) + " |",
            "|" + "|".join("---" for _ in TABLE_HEADERS) + "|",
        ]
        for item in accounts:
            values = (
                item["account_id"],
                PLATFORM_LABELS[item["platform"]],
                item["nickname"],
                item["username"],
                item["api_user_id"],
                item.get("share_url", ""),
                item.get("recorded_at", ""),
            )
            lines.append("| " + " | ".join(_escape_table_value(value) for value in values) + " |")
        try:
            temporary_path.write_text(
                "\n".join(lines) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(self.path)
        except OSError as exc:
            raise LocalAccountStoreError(f"无法写入本地账号清单：{self.path}") from exc
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _raise_invalid_markdown(self, detail: str) -> None:
        """统一报告账号 Markdown 结构错误，避免静默覆盖。"""

        raise LocalAccountStoreError(
            f"本地账号清单 Markdown 格式无效（{detail}）：{self.path}"
        )

    @staticmethod
    def _account_id(platform: str, api_user_id: str) -> str:
        """返回账号在本地清单中的稳定标识。"""

        return f"{platform.strip()}:{api_user_id.strip()}"

    @staticmethod
    def _validate_account(account: MonitorAccount) -> None:
        """校验待登记账号的稳定字段。"""

        if account.platform not in SUPPORTED_PLATFORMS:
            raise LocalAccountStoreError(f"不支持的账号平台：{account.platform}")
        if not account.api_user_id.strip():
            raise LocalAccountStoreError("API 查询 ID 不能为空。")


def _escape_table_value(value: str) -> str:
    """转义 Markdown 表格分隔符、反斜杠和换行。"""

    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text.replace("\\", "\\\\").replace("|", "\\|")


def _split_table_row(line: str) -> list[str]:
    """解析支持反斜杠转义的 Markdown 表格行。"""

    text = line.strip()
    if len(text) < 2 or not text.startswith("|") or not text.endswith("|"):
        return []
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in text[1:-1]:
        if escaped:
            if character not in {"|", "\\"}:
                current.append("\\")
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells
