"""无视他人互相 @ 的消息插件

场景：bot 开启群聊全响应时，别人 @ 别人（如 A 发"@B 吃饭去"）的消息
也会触发 AI 回复，造成打扰。本插件检测群消息中的 at 段：
- at 目标包含本 bot 时 → 正常放行（响应）
- at 目标仅为其他人时 → 静默忽略
- 无 at 时 → 放行（普通消息，交由默认逻辑处理）
管理员与白名单用户豁免。

v1.2.0 新增：
- @ 统计报表：按群记录谁最常触发忽略，/atignore 报表 查看本群 Top10
- 静默时段：quiet_hours（如 23:00-07:00，支持跨天）时段内暂停拦截、全部放行
"""

import json
import os
import re
import time
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.all import MessageChain
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import At

PLUGIN_NAME = "astrbot_plugin_ignore_at"
PLUGIN_AUTHOR = "云晓"
PLUGIN_DESC = "无视他人互相 @ 的消息（含统计报表与静默时段）"
try:
    from .hub_bridge import HUB_PROTOCOL_VERSION, emit_hub_event
except ImportError:
    from hub_bridge import HUB_PROTOCOL_VERSION, emit_hub_event

PLUGIN_VERSION = "1.3.0"

# 白名单解析缓存 TTL（秒）：配置修改后最多延迟该时长生效
_WL_CACHE_TTL = 60

# 静默时段格式校验：HH:MM-HH:MM
_QUIET_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION)
class IgnoreAtPlugin(Star):
    HUB_PROTOCOL_VERSION = HUB_PROTOCOL_VERSION
    """添加了忽略别人 @ 别人（非本 bot）的消息，管理员与白名单用户豁免。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        # 忽略统计：持久化到 plugin_data，重启不丢失
        # users 结构: {f"{群号}|{发送者ID}": {"name": 昵称, "count": 次数}}
        self.stats = {"ignored": 0, "users": {}}
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
        """从磁盘加载忽略统计（结构异常时重置，不影响运行；兼容旧版仅 ignored 键的数据）"""
        try:
            if os.path.exists(self._stats_path):
                with open(self._stats_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("ignored"), int):
                    users = data.get("users", {})
                    self.stats = {
                        "ignored": data["ignored"],
                        "users": users if isinstance(users, dict) else {},
                    }
        except Exception as e:
            logger.warning(f"【{PLUGIN_NAME}】加载忽略统计失败: {e}")

    def _save_stats(self):
        """保存忽略统计到磁盘（失败仅告警）"""
        try:
            os.makedirs(os.path.dirname(self._stats_path), exist_ok=True)
            tmp = self._stats_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.stats, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._stats_path)
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

    # ========== 静默时段（v1.2.0） ==========

    def _quiet_hours(self) -> tuple[str, str] | None:
        """解析静默时段配置（HH:MM-HH:MM，支持跨天），非法/起止相同返回 None"""
        v = str(self.config.get("quiet_hours", "") or "").strip()
        m = _QUIET_RE.match(v)
        if not m:
            return None
        start = f"{m.group(1)}:{m.group(2)}"
        end = f"{m.group(3)}:{m.group(4)}"
        if start == end:
            return None
        return (start, end)

    def _in_quiet_hours(self, now: datetime | None = None) -> bool:
        """当前时间是否处于静默时段（时段内插件暂停拦截、全部放行）"""
        qh = self._quiet_hours()
        if not qh:
            return False
        now = now or datetime.now()
        cur = now.strftime("%H:%M")
        start, end = qh
        if start < end:
            return start <= cur < end
        # 跨天区间（如 23:00-07:00）
        return cur >= start or cur < end

    # ========== 统计报表（v1.2.0） ==========

    def _record_ignored(self, event: AstrMessageEvent, sender_id: str):
        """记录一次忽略：总数 +1 并按群累计到用户排行（失败不影响拦截流程）"""
        self.stats["ignored"] += 1
        try:
            gid = ""
            try:
                gid = str(event.get_group_id() or "").strip()
            except Exception:
                pass
            if not gid:
                gid = "private"
            gkey = f"{gid}|{sender_id}"
            users = self.stats.setdefault("users", {})
            if not isinstance(users, dict):
                users = self.stats["users"] = {}
            entry = users.setdefault(gkey, {"name": "", "count": 0})
            name = ""
            try:
                name = str(event.get_sender_name() or "").strip()
            except Exception:
                pass
            if name and not entry.get("name"):
                entry["name"] = name[:20]
            entry["count"] = int(entry.get("count", 0)) + 1
            emit_hub_event(PLUGIN_NAME, "ignore_at.blocked", {"group_id": gid, "user_id": sender_id, "nickname": name}, self.context)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"【{PLUGIN_NAME}】统计记录异常: {e}")

    def _report_text(self, event: AstrMessageEvent) -> str:
        """渲染本群忽略排行报表（Top10）"""
        try:
            gid = str(event.get_group_id() or "").strip()
        except Exception:
            gid = ""
        prefix = f"{gid or 'private'}|"
        rows = []
        for k, v in (self.stats.get("users") or {}).items():
            if not isinstance(v, dict) or not k.startswith(prefix):
                continue
            try:
                count = int(v.get("count", 0))
            except (TypeError, ValueError):
                continue
            name = str(v.get("name") or k[len(prefix):])
            rows.append((count, name))
        total_users = len(rows)
        if not rows:
            return (
                "📊 忽略排行报表\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "📭 本群暂无忽略记录\n被忽略的互 @ 消息会自动计入排行"
            )
        rows.sort(key=lambda r: -r[0])
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"📊 忽略排行报表（共 {total_users} 人上榜 · 累计 {self.stats['ignored']} 条）",
                 "━━━━━━━━━━━━━━━━━━━━━━"]
        for i, (count, name) in enumerate(rows[:10]):
            icon = medals[i] if i < 3 else f"{i + 1}."
            display = name if len(name) <= 12 else name[:11] + "…"
            lines.append(f"{icon} {display} · 被忽略 {count} 次")
        top = rows[0]
        lines.append(f"👑 互 @ 大户：{top[1]}（{top[0]} 次）")
        return "\n".join(lines)

    @filter.command("atignore", alias={"忽略状态"})
    async def atignore_status(self, event: AstrMessageEvent):
        """查看本插件状态：/atignore 状态；/atignore 报表 查看忽略排行"""
        args = str(getattr(event, "message_str", "") or "").strip()
        m = re.match(r"^[\\/／]?\s*(?:atignore|忽略状态)\s*(.*)$", args, re.S)
        sub = (m.group(1) or "").strip() if m else ""
        if sub in ("帮助", "help", "?"):
            return event.chain_result([Plain(
                f"【无视互 @ 插件 v{PLUGIN_VERSION}】\n"
                "- /atignore（或 /忽略状态）：查看拦截统计与配置状态\n"
                "- /atignore 报表：查看本群互 @ 忽略排行 Top10\n"
                "- /atignore 清空：清空累计忽略统计（管理员）\n"
                "- /atignore 帮助：显示本帮助"
            )])
        if sub in ("清空", "重置", "clear", "reset"):
            try:
                is_adm = bool(event.is_admin())
            except Exception:
                is_adm = False
            if not is_adm:
                return event.chain_result([Plain("【无视互 @ 插件】仅管理员可清空统计")])
            self.stats = {"ignored": 0, "users": {}}
            self._save_stats()
            return event.chain_result([Plain("【无视互 @ 插件】已清空忽略统计数据")])
        if sub in ("报表", "排行", "排行榜"):
            return event.chain_result([Plain(self._report_text(event))])
        quiet = self._quiet_hours()
        in_quiet = self._in_quiet_hours()
        quiet_line = (
            f"- 静默时段: {quiet[0]}-{quiet[1]}（{'当前处于静默中，暂停拦截' if in_quiet else '当前不在静默时段'}）"
            if quiet
            else "- 静默时段: 未启用"
        )
        text = (
            f"【无视互 @ 插件 v{PLUGIN_VERSION}】\n"
            f"- 已忽略他人互 @ 消息: {self.stats['ignored']} 条\n"
            f"- 管理员豁免: {'开启' if self._cfg_bool('ignore_admin', True) else '关闭'}\n"
            f"- 忽略 @全体: {'开启' if self._cfg_bool('ignore_at_all', False) else '关闭'}\n"
            f"{quiet_line}\n"
            f"- 白名单: {', '.join(sorted(self._whitelist())) or '无'}\n"
            "发送 /atignore 报表 查看本群排行，/atignore 帮助 查看所有指令"
        )
        return event.chain_result([Plain(text)])

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE, priority=200)
    async def ignore_at(self, event: AstrMessageEvent):
        """仅处理群聊：他人互 @（at 目标不含本 bot）的消息静默忽略"""
        try:
            if event.is_private_chat():
                return
            # 静默时段内暂停拦截，全部放行
            if self._in_quiet_hours():
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
            self._record_ignored(event, sender_id)
            self._save_stats()
            if self._cfg_bool("log_ignored", True):
                logger.info(
                    f"已忽略他人互 @ 消息: {event.get_sender_name() or sender_id}({sender_id}) at={sorted(targets)}"
                )
            event.stop_event()
        except Exception as e:  # noqa: BLE001
            logger.exception(f"处理他人互 @ 消息异常: {e}")

    async def terminate(self):
        """插件卸载时持久化统计"""
        self._save_stats()

    def hub_status(self) -> dict:
        return {
            "version": PLUGIN_VERSION,
            "status": "online",
            "metrics": {"累计忽略": int(self.stats.get("ignored", 0)), "共享Bot数": len(getattr(self, "_hub_extra_bots", set()))},
            "actions": ["get_stats", "sync_bot_ids"],
        }

    async def hub_action(self, action: str, params: dict | None = None) -> dict:
        p = params or {}
        if action == "get_stats":
            return {"ok": True, "data": dict(self.stats)}
        if action == "sync_bot_ids":
            ids = [str(x).strip() for x in (p.get("bot_ids") or []) if str(x).strip()]
            if p.get("bot_id"):
                ids.append(str(p["bot_id"]).strip())
            if not hasattr(self, "_hub_extra_bots"):
                self._hub_extra_bots = set()
            self._hub_extra_bots.update(ids)
            return {"ok": True, "data": {"synced_bots": sorted(self._hub_extra_bots)}}
        return {"ok": False, "error": f"不支持的动作：{action}"}
