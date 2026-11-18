# -*- coding: utf-8 -*-
"""ignore_at 插件新功能测试（v1.2.0）：用户级忽略统计报表、静默时段。"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astrbot.api.message_components import At  # noqa: E402
from astrbot_plugin_ignore_at.main import IgnoreAtPlugin  # noqa: E402


class FakeMessageObj:
    def __init__(self, self_id="bot001", message=None):
        self.self_id = self_id
        self.message = message or []


class FakeEvent:
    def __init__(self, message_str="", sender_id="u1", sender_name="小明",
                 group_id="123", at_targets=None, is_admin=False, private=False):
        self.message_str = message_str
        # 使用真实 At 组件（main.py 用 isinstance 校验）
        self.message_obj = FakeMessageObj(message=[At(qq=t) for t in (at_targets or [])])
        self._sid = sender_id
        self._name = sender_name
        self._gid = group_id
        self._admin = is_admin
        self._private = private
        self.stopped = False

    def get_sender_id(self):
        return self._sid

    def get_sender_name(self):
        return self._name

    def get_group_id(self):
        return self._gid

    def is_private_chat(self):
        return self._private

    def is_admin(self):
        return self._admin

    def stop_event(self):
        self.stopped = True

    def chain_result(self, chain):
        return chain


def run(coro):
    return asyncio.run(coro)


def make_plugin(config=None, stats_dir=None):
    tmp = stats_dir or tempfile.mkdtemp(prefix="ignore_at_extra_")
    p = IgnoreAtPlugin.__new__(IgnoreAtPlugin)
    p.config = config or {}
    p.stats = {"ignored": 0, "users": {}}
    p._stats_path = os.path.join(tmp, "stats.json")
    p._wl_cache = (0.0, set())
    return p, tmp


class TestQuietHours(unittest.TestCase):
    def test_disabled_by_default(self):
        p, _ = make_plugin()
        self.assertIsNone(p._quiet_hours())
        self.assertFalse(p._in_quiet_hours())

    def test_parse_valid_range(self):
        p, _ = make_plugin({"quiet_hours": "23:00-07:00"})
        self.assertEqual(p._quiet_hours(), ("23:00", "07:00"))

    def test_same_start_end_invalid(self):
        p, _ = make_plugin({"quiet_hours": "08:00-08:00"})
        self.assertIsNone(p._quiet_hours())

    def test_dirty_format_invalid(self):
        for bad in ("abc", "25:00-26:00", "8:00-9:00x", "09:60-10:00", "12:00"):
            p, _ = make_plugin({"quiet_hours": bad})
            self.assertIsNone(p._quiet_hours(), bad)

    def test_in_quiet_normal_range(self):
        p, _ = make_plugin({"quiet_hours": "01:00-06:00"})
        self.assertTrue(p._in_quiet_hours(datetime(2026, 1, 1, 3, 30)))
        self.assertFalse(p._in_quiet_hours(datetime(2026, 1, 1, 8, 0)))
        self.assertTrue(p._in_quiet_hours(datetime(2026, 1, 1, 1, 0)))

    def test_in_quiet_cross_midnight(self):
        p, _ = make_plugin({"quiet_hours": "23:00-07:00"})
        self.assertTrue(p._in_quiet_hours(datetime(2026, 1, 1, 23, 30)))
        self.assertTrue(p._in_quiet_hours(datetime(2026, 1, 2, 2, 0)))
        self.assertTrue(p._in_quiet_hours(datetime(2026, 1, 2, 6, 59)))
        self.assertFalse(p._in_quiet_hours(datetime(2026, 1, 2, 7, 0)))
        self.assertFalse(p._in_quiet_hours(datetime(2026, 1, 2, 12, 0)))


class TestUserStats(unittest.TestCase):
    def test_record_ignored_counts_per_user_per_group(self):
        p, _ = make_plugin()
        ev1 = FakeEvent(sender_id="a", group_id="111")
        ev2 = FakeEvent(sender_id="a", group_id="111")
        ev3 = FakeEvent(sender_id="b", group_id="111")
        ev4 = FakeEvent(sender_id="a", group_id="222")
        p._record_ignored(ev1, "a")
        p._record_ignored(ev2, "a")
        p._record_ignored(ev3, "b")
        p._record_ignored(ev4, "a")
        self.assertEqual(p.stats["ignored"], 4)
        users = p.stats["users"]
        self.assertEqual(users["111|a"]["count"], 2)
        self.assertEqual(users["111|b"]["count"], 1)
        self.assertEqual(users["222|a"]["count"], 1)

    def test_record_keeps_sender_name(self):
        p, _ = make_plugin()
        p._record_ignored(FakeEvent(sender_id="u9", sender_name="爱@人怪"), "u9")
        self.assertEqual(p.stats["users"]["123|u9"]["name"], "爱@人怪")

    def test_stats_persisted_with_users(self):
        tmp = tempfile.mkdtemp(prefix="ignore_at_persist_")
        p, _ = make_plugin(stats_dir=tmp)
        p._record_ignored(FakeEvent(sender_id="k1"), "k1")
        p._save_stats()
        # 模拟重启：重新加载
        p2, _ = make_plugin(stats_dir=tmp)
        p2._load_stats()
        self.assertEqual(p2.stats["ignored"], 1)
        self.assertEqual(p2.stats["users"]["123|k1"]["count"], 1)

    def test_legacy_stats_without_users_key(self):
        tmp = tempfile.mkdtemp(prefix="ignore_at_legacy_")
        with open(os.path.join(tmp, "stats.json"), "w", encoding="utf-8") as f:
            json.dump({"ignored": 42}, f)
        p, _ = make_plugin(stats_dir=tmp)
        p._load_stats()
        self.assertEqual(p.stats["ignored"], 42)
        self.assertEqual(p.stats["users"], {})


class TestReportCommand(unittest.TestCase):
    def test_empty_report_hint(self):
        p, _ = make_plugin()
        text = p._report_text(FakeEvent())
        self.assertIn("暂无忽略记录", text)

    def test_report_sorted_top(self):
        p, _ = make_plugin()
        p.stats["users"] = {
            "123|a": {"name": "甲", "count": 5},
            "123|b": {"name": "乙", "count": 9},
            "123|c": {"name": "丙", "count": 2},
            "999|x": {"name": "外群", "count": 100},
        }
        text = p._report_text(FakeEvent(group_id="123"))
        self.assertIn("共 3 人上榜", text)
        b_pos = text.find("乙")
        a_pos = text.find("甲")
        c_pos = text.find("丙")
        self.assertTrue(b_pos < a_pos < c_pos)
        self.assertNotIn("外群", text)
        self.assertIn("互 @ 大户：乙（9 次）", text)

    def test_dispatch_report_subcommand(self):
        p, _ = make_plugin()
        r = run(p.atignore_status(FakeEvent("/atignore 报表")))
        self.assertIn("忽略排行报表", r[0].text)

    def test_status_shows_quiet_hours(self):
        p, _ = make_plugin({"quiet_hours": "23:00-07:00"})
        r = run(p.atignore_status(FakeEvent("/atignore")))
        self.assertIn("静默时段: 23:00-07:00", r[0].text)

    def test_status_default_command_still_works(self):
        p, _ = make_plugin()
        r = run(p.atignore_status(FakeEvent("/atignore")))
        self.assertIn("已忽略他人互 @ 消息", r[0].text)
        self.assertIn("未启用", r[0].text)


class TestQuietHoursInHandler(unittest.TestCase):
    def _make_handler_env(self, config=None):
        p, _ = make_plugin(config)
        return p

    def test_handler_skips_in_quiet_time(self):
        p = self._make_handler_env({"quiet_hours": "00:00-23:59"})  # 全天静默
        ev = FakeEvent(at_targets=["other1"], sender_id="x")
        run(p.ignore_at(ev))
        self.assertFalse(ev.stopped)  # 静默时段放行
        self.assertEqual(p.stats["ignored"], 0)

    def test_handler_still_blocks_outside_quiet(self):
        # 用 mock 把 _in_quiet_hours 固定为 False，避免依赖真实时钟
        p = self._make_handler_env({"quiet_hours": "03:00-04:00"})
        with mock.patch.object(IgnoreAtPlugin, "_in_quiet_hours", return_value=False):
            ev = FakeEvent(at_targets=["other1"], sender_id="y")
            run(p.ignore_at(ev))
        self.assertTrue(ev.stopped)
        self.assertEqual(p.stats["ignored"], 1)
        self.assertEqual(p.stats["users"]["123|y"]["count"], 1)

    def test_help_clear_and_terminate(self):
        p, _ = make_plugin()
        r_help = run(p.atignore_status(FakeEvent("/atignore 帮助")))
        self.assertIn("/atignore 报表", r_help[0].text)
        self.assertIn("/atignore 清空", r_help[0].text)
        p.stats = {"ignored": 3, "users": {"123|u1": {"name": "a", "count": 3}}}
        r_deny = run(p.atignore_status(FakeEvent("/atignore 清空", is_admin=False)))
        self.assertIn("仅管理员", r_deny[0].text)
        self.assertEqual(p.stats["ignored"], 3)
        r_ok = run(p.atignore_status(FakeEvent("/atignore 清空", is_admin=True)))
        self.assertIn("已清空", r_ok[0].text)
        self.assertEqual(p.stats["ignored"], 0)
        self.assertEqual(p.stats["users"], {})
        run(p.terminate())


if __name__ == "__main__":
    unittest.main(verbosity=1)
