# -*- coding: utf-8 -*-
"""补话合并 · 静默等待 + 消息合并（零 token）

做什么：
  1. 会话第一条消息先扣住，静默等 wait_seconds 秒（0 = 不等，直接放行）；
  2. 等待期间每来一条新消息就重新计时，并按序攒进缓冲；
  3. 攒够 max_messages 条或总时长超过 max_wait_seconds 就立刻放行；
  4. 放行时按 merge_separator 拼接文本，非文本组件按序补回；
  5. 之后照旧走原管线 —— 不插提示词、不改上下文、不额外调模型。

生效范围：scope_mode（private_only / group_only / all）+ 用户 / 群 / 会话三层
白名单，留空即不限制。

为什么用 CustomFilter（关键，别改）：
  WakingCheckStage 会把「任何 filter 通过的 handler」置 is_wake。若注册通配
  event_message_type 再在 handler 里 return False，群聊会变成无条件唤醒、
  对每条群消息都走一遍请求流程；挂 ScopeFilter 就能在 filter 层否掉。
"""

import asyncio
import time

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import CustomFilter, EventMessageType
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_message_merge"

DEFAULTS = {
    "enabled": True,
    "wait_seconds": 6,
    "max_wait_seconds": 18,
    "max_messages": 8,
    "merge_separator": "\\n",
    "scope_mode": "private_only",
    "user_whitelist": [],
    "group_whitelist": [],
    "chat_whitelist": [],
    "target_users": "",          # 1.x 旧字段，仍生效，等价于 user_whitelist
    "debug": False,
}

SCOPE_MODES = ("private_only", "group_only", "all")

# 数字越大越先跑
PRIORITY = 100000  # 排在 meme_manager(99999) 前，免得它 stop_event() 时把本钩子跳过

# ScopeFilter 需要在 filter 层读到当前插件实例（filter 只拿得到全局 cfg）
_ACTIVE: "Main | None" = None


def _split_csv(raw: str) -> set[str]:
    """逗号 / 空格 / 分号 / 换行 / 中文逗号分隔 → 去空集合。"""
    for sep in ("，", ",", ";", "；", " ", "\t", "\n", "\r"):
        raw = raw.replace(sep, ",")
    return {x for x in (p.strip() for p in raw.split(",")) if x}


def _flatten(val) -> list[str]:
    """配置值 → 扁平字符串列表：list、旧逗号字符串、嵌套都能吃。"""
    if val is None:
        return []
    if isinstance(val, (list, tuple, set)):
        out: list[str] = []
        for item in val:
            out.extend(_flatten(item))
        return out
    return list(_split_csv(str(val)))


def _unescape(text: str) -> str:
    """把配置里写的 \\n / \\t 还原成真实字符。"""
    return text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")


class ScopeFilter(CustomFilter):
    """会话范围过滤器：在 filter 层就挡掉不该管的会话（理由见模块 docstring）。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        inst = _ACTIVE
        return bool(inst is not None and inst.hit(event))


@register(
    PLUGIN_NAME,
    "firefly_lab",
    "补话合并：先扣住静默等待，等待期间的新消息合并成一条再喂模型（零 token）",
    "1.1.2",
)
class Main(Star):

    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._sessions: dict[str, dict] = {}
        self._csv_cache: dict[str, tuple[str, set[str]]] = {}

        global _ACTIVE
        _ACTIVE = self

    # ── 配置读取 ────────────────────────────────────────────────────────────
    def _raw(self, key: str, fallback=None):
        src = self.config if isinstance(self.config, dict) else {}
        val = src.get(key, DEFAULTS.get(key, fallback))
        if val is None or val == "":
            val = DEFAULTS.get(key, fallback)
        return val

    def _int(self, key: str, lo: int, hi: int) -> int:
        try:
            val = int(float(self._raw(key)))
        except (TypeError, ValueError):
            val = int(DEFAULTS[key])
        return max(lo, min(hi, val))

    def _csv(self, key: str) -> set[str]:
        raw = self._raw(key)
        mark = repr(raw)
        hit = self._csv_cache.get(key)
        if hit is None or hit[0] != mark:  # 配置变了才重新解析
            self._csv_cache[key] = (mark, set(_flatten(raw)))
        return self._csv_cache[key][1]

    @property
    def enabled(self) -> bool:
        return bool(self._raw("enabled"))

    @property
    def wait_seconds(self) -> int:
        return self._int("wait_seconds", 0, 300)  # 0 = 不等待，立即放行

    @property
    def max_wait_seconds(self) -> int:
        return self._int("max_wait_seconds", 1, 600)

    @property
    def max_messages(self) -> int:
        return self._int("max_messages", 1, 50)

    @property
    def debug(self) -> bool:
        return bool(self._raw("debug"))

    @property
    def separator(self) -> str:
        return _unescape(str(self._raw("merge_separator") or "\\n"))

    @property
    def scope_mode(self) -> str:
        mode = str(self._raw("scope_mode") or "private_only").strip().lower()
        return mode if mode in SCOPE_MODES else "private_only"

    @property
    def users(self) -> set[str]:
        return self._csv("user_whitelist") | self._csv("target_users")

    @property
    def groups(self) -> set[str]:
        return self._csv("group_whitelist")

    @property
    def chats(self) -> set[str]:
        return self._csv("chat_whitelist")

    # ── 是否管这个会话（同步、无 IO，可被 filter 直接调用）──────────────────
    def hit(self, event: AstrMessageEvent) -> bool:
        """总开关 + 范围 + 白名单。"""
        if not self.enabled:
            return False
        try:
            chats = self.chats
            if chats and event.unified_msg_origin not in chats:
                return False

            users = self.users
            sender = str(event.get_sender_id())

            if event.is_private_chat():
                if self.scope_mode == "group_only":
                    return False
                return (not users) or sender in users

            if self.scope_mode == "private_only":
                return False
            groups = self.groups
            if groups and str(event.get_group_id()) not in groups:
                return False
            if users and sender not in users:
                return False
            return True
        except Exception:
            logger.warning("[补话合并] 会话判定异常，本条按不处理放行", exc_info=True)
            return False

    # ── 会话状态 ────────────────────────────────────────────────────────────
    def _state(self, umo: str) -> dict:
        st = self._sessions.get(umo)
        if st is None:
            st = {
                "lock": asyncio.Lock(),
                "signal": None,      # asyncio.Event，有新补话时 set()
                "active": False,     # 当前有等待者守着这个会话
                "items": [],         # [{"text": str, "comps": [...], "ts": float}]
            }
            self._sessions[umo] = st
            if len(self._sessions) > 64:
                stale = [k for k, v in self._sessions.items() if not v["active"] and k != umo]
                for k in stale[:16]:
                    self._sessions.pop(k, None)
        return st

    # ── 一：等待期间到达的新消息 → 扣住并攒起来 ──────────────────────────────
    @filter.custom_filter(ScopeFilter)
    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE | EventMessageType.PRIVATE_MESSAGE,
        priority=PRIORITY,
    )
    async def hold_follow_up(self, event: AstrMessageEvent):
        if not self.hit(event):  # 双保险：filter 之外再挡一次
            return

        umo = event.unified_msg_origin
        st = self._sessions.get(umo)
        if st is None or not st["active"]:
            # 没有等待者：放行，让这条自己变成新的等待者
            return

        text = (event.message_str or "").strip()
        comps = [
            c for c in (event.message_obj.message or []) if not isinstance(c, Plain)
        ]
        if not text and not comps:
            return

        async with st["lock"]:
            if not st["active"]:
                return  # 等待者刚好收工，放它走
            st["items"].append({"text": text, "comps": comps, "ts": time.monotonic()})
            if st["signal"] is not None:
                st["signal"].set()
            event.stop_event()
            held = len(st["items"]) + 1

        if self.debug:
            logger.info(f"[补话合并] 扣住第 {held} 条：{text[:40]!r}")
        return

    # ── 二：模型临门一脚之前，先睡够静默期 ──────────────────────────────────
    @filter.on_waiting_llm_request(priority=PRIORITY)
    async def wait_then_merge(self, event: AstrMessageEvent) -> None:
        if not self.hit(event):
            return
        if (event.message_str or "").lstrip().startswith("/"):
            return  # 斜杠指令直接放行

        umo = event.unified_msg_origin
        st = self._state(umo)
        sig = asyncio.Event()

        async with st["lock"]:
            st["active"] = True
            st["signal"] = sig
            st["items"] = []

        started = time.monotonic()
        seen = 0
        wait = self.wait_seconds
        cap = max(self.max_wait_seconds, wait)
        maxn = self.max_messages

        try:
            while True:
                remain = min(wait, cap - (time.monotonic() - started))
                if remain <= 0:
                    break
                try:
                    await asyncio.wait_for(sig.wait(), timeout=remain)
                except asyncio.TimeoutError:
                    break  # 静默够了，放行
                async with st["lock"]:
                    sig.clear()
                    cur = len(st["items"])
                    if cur + 1 >= maxn:
                        break  # 攒够了，不等了
                    if cur == seen:
                        continue  # 陈旧信号，按剩余时间接着等
                    seen = cur
        except asyncio.CancelledError:
            raise
        finally:
            # 先把状态复位，再合并：万一 _merge 抛异常，也不会把会话永久锁死
            async with st["lock"]:
                items = st["items"]
                st["items"] = []
                st["active"] = False
                st["signal"] = None
            merged = self._merge(event, items)

        cost = time.monotonic() - started
        if merged or self.debug:
            logger.info(
                f"[补话合并] {umo} 等了 {cost:.1f}s 合并 {merged} 条"
                + (f" → {event.message_str[:60]!r}" if merged else "（无补话）")
            )

    # ── 合并动作 ────────────────────────────────────────────────────────────
    def _merge(self, event: AstrMessageEvent, items: list[dict]) -> int:
        """把缓冲文本按序拼进 event.message_str，非文本组件按序补进去。"""
        if not items:
            return 0

        parts = [event.message_str or ""]
        for it in items:
            parts.append(it["text"])
            for comp in it["comps"]:
                try:
                    event.message_obj.message.append(comp)
                except Exception:
                    logger.warning("[补话合并] 组件合并失败，已跳过", exc_info=True)

        sep = self.separator
        merged_text = sep.join(p for p in (p.strip() for p in parts) if p)
        event.message_str = merged_text
        return len(items)
