#!/usr/bin/env python3
"""26/26 单元测试套件: WhiteListManager 与核心人脉防删规则测试."""

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.whitelist import WhiteListManager, WhiteListRule


class TestWhiteListManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.config_path = self.temp_dir / "whitelist.json"
        self.manager = WhiteListManager(self.config_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # 1. 默认值规则创建
    def test_rule_creation_defaults(self):
        r = WhiteListRule(name="老婆", wxid="wxid_wife")
        self.assertEqual(r.name, "老婆")
        self.assertEqual(r.wxid, "wxid_wife")
        self.assertEqual(r.protect, "absolute")
        self.assertEqual(r.keywords, [])
        self.assertEqual(r.retain_days, 0)
        self.assertTrue(bool(r.created_at))

    # 2. 序列化与反序列化
    def test_rule_to_dict_and_from_dict(self):
        r1 = WhiteListRule(name="张总", wxid="wxid_boss", protect="retain_days", keywords=["合同", "报价"], retain_days=180)
        d = r1.to_dict()
        r2 = WhiteListRule.from_dict(d)
        self.assertEqual(r1.name, r2.name)
        self.assertEqual(r1.wxid, r2.wxid)
        self.assertEqual(r1.keywords, r2.keywords)
        self.assertEqual(r1.retain_days, r2.retain_days)

    # 3. 初始化为空
    def test_manager_init_empty(self):
        self.assertEqual(len(self.manager.list_rules()), 0)

    # 4. 基本添加规则
    def test_add_rule_basic(self):
        rule = self.manager.add("重要客户", "wxid_vip123", protect="absolute", keywords=["签约"])
        self.assertEqual(rule.name, "重要客户")
        self.assertEqual(len(self.manager.list_rules()), 1)

    # 5. 空 wxid 抛出异常
    def test_add_rule_empty_wxid_raises(self):
        with self.assertRaises(ValueError):
            self.manager.add("测试", "   ")

    # 6. 空 name 自动回退为 wxid
    def test_add_rule_empty_name_uses_wxid(self):
        rule = self.manager.add("", "wxid_auto_name")
        self.assertEqual(rule.name, "wxid_auto_name")

    # 7. 大小写不敏感检索
    def test_add_rule_case_insensitive_lookup(self):
        self.manager.add("VIP", "WXID_UPPER")
        self.assertIsNotNone(self.manager.get("wxid_upper"))
        self.assertIsNotNone(self.manager.get("vip"))

    # 8. 覆盖更新已存在规则
    def test_add_rule_update_existing(self):
        self.manager.add("小王", "wxid_wang", protect="absolute")
        self.manager.add("王总", "wxid_wang", protect="retain_days", retain_days=30)
        self.assertEqual(len(self.manager.list_rules()), 1)
        r = self.manager.get("wxid_wang")
        self.assertEqual(r.name, "王总")
        self.assertEqual(r.retain_days, 30)

    # 9. 按 wxid 删除
    def test_remove_by_wxid(self):
        self.manager.add("测试人", "wxid_del1")
        self.assertTrue(self.manager.remove("wxid_del1"))
        self.assertIsNone(self.manager.get("wxid_del1"))

    # 10. 按 name 删除
    def test_remove_by_name(self):
        self.manager.add("特定客户", "wxid_del2")
        self.assertTrue(self.manager.remove("特定客户"))
        self.assertIsNone(self.manager.get("wxid_del2"))

    # 11. 删除不存在项返回 False
    def test_remove_nonexistent_returns_false(self):
        self.assertFalse(self.manager.remove("not_exist_item"))

    # 12. 按 wxid 获取规则
    def test_get_rule_by_wxid(self):
        self.manager.add("伙伴", "wxid_partner")
        rule = self.manager.get("wxid_partner")
        self.assertIsNotNone(rule)
        self.assertEqual(rule.name, "伙伴")

    # 13. 按 name 获取规则
    def test_get_rule_by_name(self):
        self.manager.add("伙伴B", "wxid_partner_b")
        rule = self.manager.get("伙伴b")
        self.assertIsNotNone(rule)
        self.assertEqual(rule.wxid, "wxid_partner_b")

    # 14. 规则列表列出
    def test_list_rules(self):
        self.manager.add("A", "wxid_a")
        self.manager.add("B", "wxid_b")
        rules = self.manager.list_rules()
        self.assertEqual(len(rules), 2)

    # 15. 清空规则
    def test_clear_rules(self):
        self.manager.add("A", "wxid_a")
        self.manager.clear()
        self.assertEqual(len(self.manager.list_rules()), 0)

    # 16. 持久化存储保存与重新加载
    def test_persistence_save_and_load(self):
        self.manager.add("持久化测试", "wxid_persist", protect="absolute", keywords=["账单"])
        mgr2 = WhiteListManager(self.config_path)
        self.assertEqual(len(mgr2.list_rules()), 1)
        self.assertIsNotNone(mgr2.get("wxid_persist"))

    # 17. 损坏文件容错处理
    def test_corrupted_config_file_handled_gracefully(self):
        self.config_path.write_text("invalid json content {{{")
        mgr = WhiteListManager(self.config_path)
        self.assertEqual(len(mgr.list_rules()), 0)

    # 18. 空规则时不保护
    def test_is_protected_when_empty_returns_false(self):
        prot, _ = self.manager.is_protected("/path/to/random_file.pdf")
        self.assertFalse(prot)

    # 19. 路径分段中命中 wxid
    def test_is_protected_wxid_in_path_parts(self):
        self.manager.add("老婆", "wxid_sweetheart")
        p = Path("/Users/me/WeChat/wxid_sweetheart/msg/video/1.mp4")
        prot, reason = self.manager.is_protected(p)
        self.assertTrue(prot)
        self.assertIn("老婆", reason)

    # 20. 路径字符串中命中 wxid
    def test_is_protected_wxid_in_path_string(self):
        self.manager.add("重要群", "18923489@chatroom")
        p = "/data/xwechat_files/msg/attach/18923489@chatroom_att.dat"
        prot, reason = self.manager.is_protected(p)
        self.assertTrue(prot)
        self.assertIn("重要群", reason)

    # 21. 文件名中命中联系人名称
    def test_is_protected_name_in_filename(self):
        self.manager.add("李总", "wxid_lizong")
        p = "/downloads/李总_财务报表_2026.xlsx"
        prot, reason = self.manager.is_protected(p)
        self.assertTrue(prot)
        self.assertIn("李总", reason)

    # 22. 目录名中包含联系人名称
    def test_is_protected_name_in_directory_name(self):
        self.manager.add("家人", "wxid_family")
        p = "/storage/家人/family_photo.jpg"
        prot, reason = self.manager.is_protected(p)
        self.assertTrue(prot)
        self.assertIn("家人", reason)

    # 23. 关键词匹配文件名
    def test_is_protected_keywords_matching(self):
        self.manager.add("商务组", "wxid_biz", keywords=["合同", "保密协议"])
        p = "/files/重要签约合同终版.pdf"
        prot, reason = self.manager.is_protected(p)
        self.assertTrue(prot)
        self.assertIn("商务组", reason)

    # 24. 关键词大小写不敏感
    def test_is_protected_keywords_case_insensitive(self):
        self.manager.add("设计部", "wxid_design", keywords=["NDA", "FIGMA"])
        p = "/files/project_nda_signed.pdf"
        prot, reason = self.manager.is_protected(p)
        self.assertTrue(prot)

    # 25. 保留天数过期后不保护
    def test_is_protected_retain_days_expired(self):
        self.manager.add("临时客户", "wxid_temp", protect="retain_days", retain_days=30, keywords=["临时文件"])
        # 40 天前的文件
        old_mtime = (datetime.now() - timedelta(days=40)).timestamp()
        p = "/files/临时文件_demo.mp4"
        prot, _ = self.manager.is_protected(p, mtime=old_mtime)
        self.assertFalse(prot)

    # 26. 保留天数内受保护
    def test_is_protected_retain_days_unexpired(self):
        self.manager.add("合作方", "wxid_partner", protect="retain_days", retain_days=30, keywords=["合作策划"])
        # 10 天前的文件
        recent_mtime = (datetime.now() - timedelta(days=10)).timestamp()
        p = "/files/合作策划_草案.docx"
        prot, reason = self.manager.is_protected(p, mtime=recent_mtime)
        self.assertTrue(prot)
        self.assertIn("保留 30 天内文件", reason)


if __name__ == "__main__":
    unittest.main()
