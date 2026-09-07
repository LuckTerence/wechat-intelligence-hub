#!/usr/bin/env python3
"""WeChat WhiteList Manager - 核心联系人与重要会话防删白名单系统.

负责核心人脉（家人、重要客户、重点工作群）的标签化管理，
提供文件防删校验规则，确保核心聊天资料与文件永不被误删。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


@dataclass
class WhiteListRule:
    """白名单规则项."""
    name: str                           # 联系人或群聊名称 (如: "老婆", "重要客户A")
    wxid: str                           # 微信 ID 或群聊 ID (如: "wxid_xxx", "xxx@chatroom")
    protect: str = "absolute"           # 保护级别: "absolute" (绝对保护永不删) 或 "retain_days" (保留指定天数)
    keywords: List[str] = field(default_factory=list)  # 文件名关键词匹配 (如: ["合同", "宝宝", "结婚"])
    retain_days: int = 0                # 当 protect 为 retain_days 时的有效天数 (0 为不限制)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WhiteListRule:
        return cls(
            name=data.get("name", ""),
            wxid=data.get("wxid", ""),
            protect=data.get("protect", "absolute"),
            keywords=data.get("keywords", []),
            retain_days=int(data.get("retain_days", 0)),
            created_at=data.get("created_at", datetime.now().isoformat()),
        )


class WhiteListManager:
    """白名单管理器，支持持久化存储与文件级防删匹配."""

    def __init__(self, config_path: Optional[Union[str, Path]] = None):
        if config_path:
            self.config_path = Path(config_path).expanduser().resolve()
        else:
            self.config_path = Path.home() / ".wechat_slim_whitelist.json"

        self.rules: Dict[str, WhiteListRule] = {}  # key: wxid (标准化小写)
        self.name_index: Dict[str, str] = {}       # key: name (小写) -> wxid
        self.load()

    def load(self) -> None:
        """从 JSON 配置文件加载白名单规则."""
        self.rules.clear()
        self.name_index.clear()
        if not self.config_path.exists():
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                rules_list = data.get("rules", [])
                for r_data in rules_list:
                    rule = WhiteListRule.from_dict(r_data)
                    key = rule.wxid.strip().lower()
                    if key:
                        self.rules[key] = rule
                        if rule.name:
                            self.name_index[rule.name.strip().lower()] = key
        except Exception:
            # 容错处理: 若文件损坏，不中断主流程
            self.rules = {}
            self.name_index = {}

    def save(self) -> None:
        """持久化保存白名单规则到文件."""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": "1.0",
                "updated_at": datetime.now().isoformat(),
                "rules": [r.to_dict() for r in self.rules.values()],
            }
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def add(
        self,
        name: str,
        wxid: str,
        protect: str = "absolute",
        keywords: Optional[List[str]] = None,
        retain_days: int = 0,
    ) -> WhiteListRule:
        """添加或更新白名单规则."""
        clean_wxid = wxid.strip()
        clean_name = name.strip()
        if not clean_wxid:
            raise ValueError("wxid 不能为空")
        if not clean_name:
            clean_name = clean_wxid

        key = clean_wxid.lower()
        rule = WhiteListRule(
            name=clean_name,
            wxid=clean_wxid,
            protect=protect,
            keywords=[k.strip() for k in (keywords or []) if k.strip()],
            retain_days=retain_days,
        )
        self.rules[key] = rule
        self.name_index[clean_name.lower()] = key
        self.save()
        return rule

    def remove(self, identifier: str) -> bool:
        """按 wxid 或名称移除白名单规则."""
        target_key = identifier.strip().lower()
        # 1. 直接按 wxid 匹配
        if target_key in self.rules:
            rule = self.rules.pop(target_key)
            if rule.name.lower() in self.name_index:
                del self.name_index[rule.name.lower()]
            self.save()
            return True

        # 2. 按 name 匹配
        if target_key in self.name_index:
            real_wxid = self.name_index.pop(target_key)
            if real_wxid in self.rules:
                del self.rules[real_wxid]
            self.save()
            return True

        return False

    def get(self, identifier: str) -> Optional[WhiteListRule]:
        """按 wxid 或 name 获取规则."""
        target_key = identifier.strip().lower()
        if target_key in self.rules:
            return self.rules[target_key]
        if target_key in self.name_index:
            return self.rules.get(self.name_index[target_key])
        return None

    def list_rules(self) -> List[WhiteListRule]:
        """列出所有白名单规则."""
        return list(self.rules.values())

    def clear(self) -> None:
        """清空所有白名单规则."""
        self.rules.clear()
        self.name_index.clear()
        self.save()

    def is_protected(
        self,
        file_path: Union[str, Path],
        mtime: float = 0.0,
    ) -> Tuple[bool, Optional[str]]:
        """检查指定文件是否受到白名单保护.

        返回: (是否保护, 保护原因)
        """
        if not self.rules:
            return False, None

        fp = Path(file_path)
        path_str = str(fp).lower()
        filename = fp.name.lower()
        parts = [p.lower() for p in fp.parts]

        now_ts = datetime.now().timestamp()

        for rule in self.rules.values():
            r_wxid = rule.wxid.lower()
            r_name = rule.name.lower()

            matched = False
            # 1. 路径中包含 wxid (常见于按照联系人 ID 分目录存储)
            if r_wxid in parts or r_wxid in path_str:
                matched = True

            # 2. 路径或文件名中包含联系人姓名
            elif r_name and (r_name in parts or r_name in filename):
                matched = True

            # 3. 文件名命中该规则的保护关键词
            elif rule.keywords:
                for kw in rule.keywords:
                    if kw.lower() in filename:
                        matched = True
                        break

            if matched:
                # 检查保护级别
                if rule.protect == "absolute":
                    return True, f"白名单保护: {rule.name} ({rule.wxid}) [绝对保护]"
                elif rule.protect == "retain_days" and rule.retain_days > 0:
                    file_age_days = (now_ts - mtime) / 86400 if mtime > 0 else 0
                    if file_age_days <= rule.retain_days:
                        return True, f"白名单保护: {rule.name} ({rule.wxid}) [保留 {rule.retain_days} 天内文件]"

        return False, None
