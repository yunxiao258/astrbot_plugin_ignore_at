"""无视他人互相 @ 的消息插件

场景：bot 开启群聊全响应时，别人 @ 别人（如 A 发“@B 吃饭去”）的消息
也会触发 AI 回复，造成打扰。本插件检测消息中的 at 段：
- at 目标包含本 bot 时 → 正常放行（响应）
- at 目标仅为其他人时 → 静默忽略
- 无 at 时 → 放行（普通消息，交由默认逻辑处理）
管理员与白名单用户豁免。
"""

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register


@register("astrbot_plugin_ignore_at", "Administrator", "无视他人互相 @ 的消息", "1.0.0")
class IgnoreAtPlugin(Star):
    """添加了忽略别人 @ 别人（非本 bot）的消息，管理员与白名单用户豁免。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.stats = {"ignored": 0}

    @staticmethod
    def _self_id(event: AstrMessageEvent) -> str:
        return str(getattr(event.message_obj, "self_id", "") or "")

    def _at_targets(self, event: AstrMessageEvent) -> set[str]:
        targets: set[str] = set()
        message = getattr(event.message_obj, "message", None)
        if not message:
            return targets
        for seg in message:
            if getattr(seg, "type", "") != "at":
                continue
            qq = str((getattr(seg, "data", None) or {}).get("qq", ""))
            if not qq:
                continue
            targets.add(qq)
        return targets

    def _is_effective_call(self, targets: set[str], self_id: str) -> bool:
        if "all" in targets:
            return not self.config.get("ignore_at_all", False)
        return self_id in targets

    def _is_exempt(self, event: AstrMessageEvent, sender_id: str) -> bool:
        if self.config.get("ignore_admin", True) and event.is_admin():
            return True
        whitelist = {
            x.strip()
            for x in str(self.config.get("whitelist_ids", "")).split(",")
            if x.strip()
        }
        return sender_id in whitelist

    @filter.event_message_type(EventMessageType.ALL, priority=200)
    async def ignore_at(self, event: AstrMessageEvent):
        try:
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
            if self.config.get("log_ignored", True):
                logger.info(
                    f"已忽略他人互 @ 消息: {event.get_sender_name() or sender_id}({sender_id}) at={sorted(targets)}"
                )
            event.stop_event()
        except Exception as e:
            logger.error(f"处理他人互 @ 消息异常: {e}")
