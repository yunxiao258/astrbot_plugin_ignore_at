# -*- coding: utf-8 -*-
"""无视他人互相 @ 插件单元测试：at 目标识别、有效呼叫判定、豁免、统计持久化、脏值防御

使用内存替身（FakeEvent/FakeMessageObj）与临时目录持久化，不联网、不写真实 plugin_data。
"""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astrbot_plugin_ignore_at.main import IgnoreAtPlugin, _WL_CACHE_TTL  # noqa: E402

from astrbot.core.message.components import At, AtAll, Plain  # noqa: E402


class FakeMessageObj:
    """最小 message_obj 替身：含组件列表与 self_id"""

    def __init__(self, segments=None, self_id="12345"):
        self.message = segments or []
        self.self_id = self_id


class FakeEvent:
    """最小事件替身：支持忽略逻辑所需的全部方法"""

    def __init__(
        self,
        segments=None,
        self_id="12345",
        private=False,
        is_admin=False,
        sender_id="10001",
        sender_name="小明",
    ):
        self.message_obj = FakeMessageObj(segments, self_id)
        self._private = private
        self._is_admin = is_admin
        self.sender_id = sender_id
        self.sender_name = sender_name
        self.stopped = False

    def is_private_chat(self):
        return self._private

    def get_sender_id(self):
        return self.sender_id

    def get_sender_name(self):
        return self.sender_name

    def is_admin(self):
        return self._is_admin

    def stop_event(self):
        self.stopped = True

    def chain_result(self, chain):
        return chain


def make_plugin(config=None):
    """构造未初始化实例（绕过文件 IO），持久化注入临时目录"""
    p = IgnoreAtPlugin.__new__(IgnoreAtPlugin)
    p.config = config or {}
    p.stats = {"ignored": 0}
    p._stats_path = os.path.join(tempfile.mkdtemp(), "stats.json")
    p._wl_cache = (0.0, set())
    return p


async def run(coro):
    """运行异步协程"""
    return await coro


def run_sync(coro):
    """同步执行异步协程（测试用）"""
    return asyncio.run(run(coro))


class TestAtTargets(unittest.TestCase):
    """at 目标识别（组件对象格式）"""

    def test_plain_at_component(self):
        p = make_plugin()
        event = FakeEvent(segments=[Plain("吃饭去"), At(qq="22222")])
        self.assertEqual(p._at_targets(event), {"22222"})

    def test_int_qq_converted(self):
        p = make_plugin()
        event = FakeEvent(segments=[At(qq=22222)])
        self.assertEqual(p._at_targets(event), {"22222"})

    def test_at_all_component(self):
        p = make_plugin()
        event = FakeEvent(segments=[AtAll()])
        self.assertEqual(p._at_targets(event), {"all"})

    def test_mixed_targets(self):
        p = make_plugin()
        event = FakeEvent(segments=[At(qq="12345"), At(qq="22222")])
        self.assertEqual(p._at_targets(event), {"12345", "22222"})

    def test_no_at_segment(self):
        p = make_plugin()
        event = FakeEvent(segments=[Plain("你好")])
        self.assertEqual(p._at_targets(event), set())

    def test_empty_message(self):
        p = make_plugin()
        event = FakeEvent(segments=[])
        self.assertEqual(p._at_targets(event), set())


class TestEffectiveCall(unittest.TestCase):
    """有效呼叫判定"""

    def test_at_self_is_call(self):
        p = make_plugin()
        self.assertTrue(p._is_effective_call({"12345"}, "12345"))

    def test_at_other_not_call(self):
        p = make_plugin()
        self.assertFalse(p._is_effective_call({"22222"}, "12345"))

    def test_at_all_default_pass(self):
        # ignore_at_all 默认关闭 → @全体 视为有效呼叫（放行）
        p = make_plugin()
        self.assertTrue(p._is_effective_call({"all"}, "12345"))

    def test_at_all_ignored_when_enabled(self):
        p = make_plugin({"ignore_at_all": True})
        self.assertFalse(p._is_effective_call({"all"}, "12345"))

    def test_mixed_contains_self(self):
        p = make_plugin()
        self.assertTrue(p._is_effective_call({"12345", "22222"}, "12345"))


class TestIgnore(unittest.TestCase):
    """整体忽略行为"""

    def test_at_other_ignored(self):
        p = make_plugin()
        event = FakeEvent(segments=[Plain("吃饭去"), At(qq="22222")])
        run_sync(p.ignore_at(event))
        self.assertTrue(event.stopped)
        self.assertEqual(p.stats["ignored"], 1)

    def test_at_self_released(self):
        p = make_plugin()
        event = FakeEvent(segments=[At(qq="12345")])
        run_sync(p.ignore_at(event))
        self.assertFalse(event.stopped)

    def test_at_all_default_released(self):
        p = make_plugin()
        event = FakeEvent(segments=[AtAll()])
        run_sync(p.ignore_at(event))
        self.assertFalse(event.stopped)

    def test_at_all_ignored_when_enabled(self):
        p = make_plugin({"ignore_at_all": True})
        event = FakeEvent(segments=[AtAll()])
        run_sync(p.ignore_at(event))
        self.assertTrue(event.stopped)
        self.assertEqual(p.stats["ignored"], 1)

    def test_mixed_at_released(self):
        p = make_plugin()
        event = FakeEvent(segments=[At(qq="12345"), At(qq="22222")])
        run_sync(p.ignore_at(event))
        self.assertFalse(event.stopped)

    def test_no_at_released(self):
        p = make_plugin()
        event = FakeEvent(segments=[Plain("你好")])
        run_sync(p.ignore_at(event))
        self.assertFalse(event.stopped)

    def test_admin_exempt(self):
        p = make_plugin()
        event = FakeEvent(segments=[At(qq="22222")], is_admin=True)
        run_sync(p.ignore_at(event))
        self.assertFalse(event.stopped)
        self.assertEqual(p.stats["ignored"], 0)

    def test_whitelist_exempt(self):
        p = make_plugin({"whitelist_ids": "10001"})
        event = FakeEvent(segments=[At(qq="22222")])
        run_sync(p.ignore_at(event))
        self.assertFalse(event.stopped)
        self.assertEqual(p.stats["ignored"], 0)

    def test_private_chat_skipped(self):
        p = make_plugin()
        event = FakeEvent(segments=[At(qq="22222")], private=True)
        run_sync(p.ignore_at(event))
        self.assertFalse(event.stopped)

    def test_empty_sender_skipped(self):
        p = make_plugin()
        event = FakeEvent(segments=[At(qq="22222")], sender_id="")
        run_sync(p.ignore_at(event))
        self.assertFalse(event.stopped)


class TestStatsPersist(unittest.TestCase):
    """统计持久化：重启后不丢失"""

    def test_save_and_reload(self):
        path = os.path.join(tempfile.mkdtemp(), "stats.json")
        p1 = make_plugin()
        p1._stats_path = path
        p1.stats["ignored"] = 5
        p1._save_stats()
        p2 = make_plugin()
        p2._stats_path = path
        p2._load_stats()
        self.assertEqual(p2.stats["ignored"], 5)

    def test_corrupt_stats_reset(self):
        path = os.path.join(tempfile.mkdtemp(), "stats.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not-json{{{")
        p = make_plugin()
        p._stats_path = path
        p._load_stats()
        self.assertEqual(p.stats["ignored"], 0)


class TestWhitelistCache(unittest.TestCase):
    """白名单解析缓存"""

    def test_cache_ttl(self):
        p = make_plugin()
        p.config["whitelist_ids"] = "10001, 10002,"
        wl = p._whitelist()
        self.assertEqual(wl, {"10001", "10002"})
        # 缓存命中：修改配置后 TTL 内仍返回旧值
        p.config["whitelist_ids"] = "99999"
        self.assertEqual(p._whitelist(), {"10001", "10002"})
        # 过期后重新解析
        p._wl_cache = (p._wl_cache[0] - _WL_CACHE_TTL - 1, p._wl_cache[1])
        self.assertEqual(p._whitelist(), {"99999"})


class TestDirtyConfig(unittest.TestCase):
    """脏值防御"""

    def test_cfg_bool_string(self):
        p = make_plugin({"ignore_admin": "false"})
        self.assertFalse(p._cfg_bool("ignore_admin", True))
        p.config["ignore_admin"] = "true"
        self.assertTrue(p._cfg_bool("ignore_admin", True))
        p.config["ignore_admin"] = 0
        self.assertFalse(p._cfg_bool("ignore_admin", True))
        p.config["ignore_admin"] = "garbage"
        self.assertTrue(p._cfg_bool("ignore_admin", True))  # 脏值回退默认

    def test_whitelist_dirty(self):
        p = make_plugin({"whitelist_ids": 12345})
        self.assertEqual(p._whitelist(), set())


class TestStatusCommand(unittest.TestCase):
    """状态查询指令"""

    def test_status_text(self):
        p = make_plugin()
        p.stats["ignored"] = 3
        event = FakeEvent()
        result = run_sync(p.atignore_status(event))
        text = "\n".join(seg.text for seg in result)
        self.assertIn("3", text)
        self.assertIn("已忽略他人互 @ 消息", text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
