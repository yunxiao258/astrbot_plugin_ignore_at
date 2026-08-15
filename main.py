"""无视他人互相 @ 的消息插件

场景：bot 开启群聊全响应时，别人 @ 别人（如 A 发"@B 吃饭去"）的消息
也会触发 AI 回复，造成打扰。本插件检测群消息中的 at 段：
- at 目标包含本 bot 时 → 正常放行（响应）
- at 目标仅为其他人时 → 静默忽略
- 无 at 时 → 放行（普通消息，交由默认逻辑处理）
管理员与白名单用户豁免。
"""

import json
import os
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.all import MessageChain
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import At

PLUGIN_NAME = "astrbot_plugin_ignore_at"
PLUGIN_AUTHOR = "Administrator"
PLUGIN_DESC = "无视他人互相 @ 的消息"
PLUGIN_VERSION = "1.1.0"

# 白名单解析缓存 TTL（秒）：配置修改后最多延迟该时长生效
_WL_CACHE_TTL = 60


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION)
class IgnoreAtPlugin(Star):
    """添加了忽略别人 @ 别人（非本 bot）的消息，管理员与白名单用户豁免。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        # 忽略统计：持久化到 plugin_data，重启不丢失
        self.stats = {"ignored": 0}
        self._stats_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "plugin_data",
            PLUGIN_NAME,
            "stats.json",
        )
        self._load_stats()
        # 白名单解析缓存: (时间戳, 集合)，配置修改后最多缓存 60 秒
        self._wl_cache: tuple[float, set[str]] = (0.0, set())

    # ========== 配置防御（脏值容错） ==========

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        """防御性读取布尔配置，脏值回退默认"""
        v = self.config.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("1", "true", "yes", "on"):
                return True
            if s in ("0", "false", "no", "off"):
                return False
        return default

    # ========== 统计持久化 ==========

    def _load_stats(self):
        """从磁盘加载忽略统计（结构异常时重置，不影响运行）"""
        try:
            if os.path.exists(self._stats_path):
                with open(self._stats_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("ignored"), int):
                    self.stats = {"ignored": data["ignored"]}
        except Exception as e:
            logger.warning(f"【{PLUGIN_NAME}】加载忽略统计失败: {e}")

    def _save_stats(self):
        """保存忽略统计到磁盘（失败仅告警）"""
        try:
            os.makedirs(os.path.dirname(self._stats_path), exist_ok=True)
            with open(self._stats_path, "w", encoding="utf-8") as f:
                json.dump(self.stats, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"【{PLUGIN_NAME}】保存忽略统计失败: {e}")

    # ========== 核心逻辑 ==========

    @staticmethod
    def _self_id(event: AstrMessageEvent) -> str:
        return str(getattr(event.message_obj, "self_id", "") or "")

    def _at_targets(self, event: AstrMessageEvent) -> set[str]:
        """收集消息中的 at 目标集合。

        平台消息段为 At 组件对象（AtAll 是其子类，qq 恒为 "all"），
        qq 是组件直接属性而非 dict。
        """
        targets: set[str] = set()
        message = getattr(event.message_obj, "message", None)
        if not message:
            return targets
        for seg in message:
            if not isinstance(seg, At):
                continue
            qq = str(getattr(seg, "qq", "") or "")
            if not qq:
                continue
            targets.add(qq)
        return targets

    def _is_effective_call(self, targets: set[str], self_id: str) -> bool:
        """at 目标是否为有效呼叫（含本 bot，或 @全体 且未开启忽略 @全体）"""
        if "all" in targets:
            return not self._cfg_bool("ignore_at_all", False)
        return self_id in targets

    def _whitelist(self) -> set[str]:
        """白名单集合（TTL 缓存，避免每条消息重建）"""
        ts, cached = self._wl_cache
        if cached and time.time() - ts < _WL_CACHE_TTL:
            return cached
        v = self.config.get("whitelist_ids", "")
        if isinstance(v, (list, tuple)):
            wl = {str(x).strip() for x in v if str(x).strip()}
        elif isinstance(v, str):
            wl = {x.strip() for x in v.split(",") if x.strip()}
        else:
            wl = set()
        self._wl_cache = (time.time(), wl)
        return wl

    def _is_exempt(self, event: AstrMessageEvent, sender_id: str) -> bool:
        """豁免：管理员（默认开）或白名单成员"""
        if self._cfg_bool("ignore_admin", True) and event.is_admin():
            return True
        return sender_id in self._whitelist()

    @filter.command("atignore", alias={"忽略状态"})
    async def atignore_status(self, event: AstrMessageEvent):
        """查看本插件拦截统计：/atignore"""
        text = (
            "【无视互 @ 插件】\n"
            f"- 已忽略他人互 @ 消息: {self.stats['ignored']} 条\n"
            f"- 管理员豁免: {'开启' if self._cfg_bool('ignore_admin', True) else '关闭'}\n"
            f"- 忽略 @全体: {'开启' if self._cfg_bool('ignore_at_all', False) else '关闭'}\n"
            f"- 白名单: {', '.join(sorted(self._whitelist())) or '无'}"
        )
        return event.chain_result([Plain(text)])

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE, priority=200)
    async def ignore_at(self, event: AstrMessageEvent):
        """仅处理群聊：他人互 @（at 目标不含本 bot）的消息静默忽略"""
        try:
            if event.is_private_chat():
                return
            sender_id = str(event.get_sender_id())
            if not sender_id:
                return
            self_id = self._self_id(event)
            targets = self._at_targets(event)
            if not targets:
                return
            if self._is_effective_call(targets, self_id):
                return
            if self._is_exempt(event, sender_id):
                return
            self.stats["ignored"] += 1
            self._save_stats()
            if self._cfg_bool("log_ignored", True):
                logger.info(
                    f"已忽略他人互 @ 消息: {event.get_sender_name() or sender_id}({sender_id}) at={sorted(targets)}"
                )
            event.stop_event()
        except Exception as e:  # noqa: BLE001
            logger.exception(f"处理他人互 @ 消息异常: {e}")
