"""AstrBot 插件：群关键词自助提醒。

任何群成员都可以在群里为自己订阅关键词：

    /设置关键词提醒 紧急

之后该群里只要有人说到「紧急」，机器人就会私聊提醒订阅者本人。
关键词只在发送指令的那个群生效，不会跨群打扰。

支持的指令：
    /设置关键词提醒 <关键词>   订阅（也支持 /正则/ 形式）
    /我的关键词提醒            查看自己在所有群订阅了什么
    /删除关键词提醒 <关键词>   取消订阅某一个
    /清空关键词提醒            清空自己在当前群的全部订阅

设计要点：
- 订阅数据按「群 → 用户 → 关键词列表」三级组织，命中查询只需要遍历本群的订阅者
- 数据落在 data/plugin_data/astrbot_plugin_keyword_watch/subscriptions.json，重启不丢
- 写操作加锁 + 原子替换写盘，避免并发写坏文件
- 只监听群消息、不 stop_event，不影响群里正常的 LLM 对话
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

try:  # StarTools 在较新版本才导出，老版本走下面的兜底路径
    from astrbot.api.star import StarTools
except ImportError:  # pragma: no cover
    StarTools = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_keyword_watch"
DATA_VERSION = 1

DEFAULT_TEMPLATE = (
    "【关键词提醒】\n"
    "群：{group_name}（{group}）\n"
    "发送人：{sender}（{sender_id}）\n"
    "时间：{time}\n"
    "命中：{keywords}\n"
    "内容：{text}"
)

CMD_ADD = "设置关键词提醒"
CMD_LIST = "我的关键词提醒"
CMD_DEL = "删除关键词提醒"
CMD_CLEAR = "清空关键词提醒"
CMD_STATS = "关键词提醒统计"

# 判断「这是不是一条指令消息」时用到的所有指令名
COMMAND_NAMES = (
    CMD_ADD,
    "添加关键词提醒",
    CMD_LIST,
    CMD_DEL,
    CMD_CLEAR,
    CMD_STATS,
)

HELP_TEXT = (
    "关键词提醒用法：\n"
    f"/{CMD_ADD} 紧急      订阅关键词「紧急」\n"
    f"/{CMD_ADD} /订单\\d+/   用正则订阅\n"
    f"/{CMD_LIST}           查看我订阅的关键词\n"
    f"/{CMD_DEL} 紧急      取消订阅\n"
    f"/{CMD_CLEAR}          清空本群的全部订阅\n"
    "关键词只在发送指令的这个群生效，命中后会私聊提醒你。"
)


class _SafeDict(dict):
    """缺失的模板变量保持原样输出，避免 str.format_map 抛 KeyError。"""

    def __missing__(self, key: str) -> str:  # noqa: D105
        return "{" + key + "}"


class KeywordWatchPlugin(Star):
    """群关键词自助提醒插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config: dict[str, Any] = config or {}

        # 数据文件
        self._data_dir = self._resolve_data_dir()
        self._data_path = self._data_dir / "subscriptions.json"
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = self._load_data()

        # 正则关键词编译缓存，key 是 pattern
        self._regex_cache: dict[str, re.Pattern[str] | None] = {}
        # 通知冷却：(群号, 用户QQ) -> 上次通知时间戳
        self._last_notify: dict[tuple[str, str], float] = {}

        total = self._count_keywords()
        logger.info(
            f"[keyword_watch] 已加载，共 {len(self._data.get('groups', {}))} 个群、"
            f"{total} 个关键词订阅，数据文件：{self._data_path}"
        )

    # ------------------------------------------------------------------
    # 数据持久化
    # ------------------------------------------------------------------
    def _resolve_data_dir(self) -> Path:
        """定位插件数据目录，优先用框架提供的 StarTools。"""
        if StarTools is not None:
            try:
                return Path(StarTools.get_data_dir(PLUGIN_NAME))
            except TypeError:
                try:
                    return Path(StarTools.get_data_dir())  # 老签名，靠调用栈推断
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[keyword_watch] StarTools 取数据目录失败: {exc}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[keyword_watch] StarTools 取数据目录失败: {exc}")

        try:
            from astrbot.core.utils.astrbot_path import (  # noqa: PLC0415
                get_astrbot_data_path,
            )

            return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        except Exception:  # noqa: BLE001
            fallback = Path.cwd() / "data" / "plugin_data" / PLUGIN_NAME
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    def _empty_data(self) -> dict[str, Any]:
        return {"version": DATA_VERSION, "groups": {}, "group_names": {}}

    def _load_data(self) -> dict[str, Any]:
        """读取订阅文件；文件损坏时备份并重建，不让插件起不来。"""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error(f"[keyword_watch] 创建数据目录失败: {exc}")

        if not self._data_path.exists():
            return self._empty_data()

        try:
            with open(self._data_path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            backup = self._data_path.with_name(
                f"subscriptions.broken.{int(time.time())}.json"
            )
            try:
                os.replace(self._data_path, backup)
                logger.error(
                    f"[keyword_watch] 订阅文件损坏（{exc}），已备份到 {backup.name} 并重建"
                )
            except OSError:
                logger.error(f"[keyword_watch] 订阅文件损坏且无法备份: {exc}")
            return self._empty_data()

        if not isinstance(data, dict) or not isinstance(data.get("groups"), dict):
            logger.error("[keyword_watch] 订阅文件格式异常，已重置")
            return self._empty_data()

        data.setdefault("version", DATA_VERSION)
        data.setdefault("group_names", {})
        return data

    def _write_file(self) -> None:
        """原子写入：先写临时文件再替换，避免中途崩溃留下半个文件。"""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            temp_path = self._data_path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2)
            os.replace(temp_path, self._data_path)
        except OSError as exc:
            logger.error(f"[keyword_watch] 写入订阅文件失败: {exc}")

    async def _save(self) -> None:
        """在锁内调用，把写盘放到线程里避免阻塞事件循环。"""
        await asyncio.to_thread(self._write_file)

    # ------------------------------------------------------------------
    # 配置辅助
    # ------------------------------------------------------------------
    def _int_conf(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _bool_conf(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}

    def _now_str(self) -> str:
        offset = float(self.config.get("timezone_offset", 8) or 8)
        try:
            offset = float(offset)
        except (TypeError, ValueError):
            offset = 8.0
        return (datetime.now(timezone.utc) + timedelta(hours=offset)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    def _render(self, template: str, context: dict[str, Any]) -> str:
        try:
            return template.format_map(_SafeDict(context))
        except (ValueError, IndexError) as exc:
            logger.warning(f"[keyword_watch] 模板渲染失败，已原样发送: {exc}")
            return template

    def _truncate(self, content: str) -> str:
        limit = self._int_conf("max_content_length", 1500)
        if limit <= 0 or len(content) <= limit:
            return content
        return content[:limit] + "\n……（内容过长已截断）"

    # ------------------------------------------------------------------
    # 关键词解析与订阅操作
    # ------------------------------------------------------------------
    def _parse_keyword(self, raw: str) -> tuple[str | None, str, str]:
        """解析用户输入的关键词。

        Returns:
            (规范化后的关键词, 类型 text/regex, 错误信息)。出错时关键词为 None。
        """
        raw = (raw or "").strip()
        if not raw:
            return None, "", "关键词不能为空"

        max_len = self._int_conf("max_keyword_length", 50)

        # /xxx/ 形式按正则处理
        if len(raw) >= 3 and raw.startswith("/") and raw.endswith("/"):
            pattern = raw[1:-1]
            if not pattern.strip():
                return None, "", "正则内容不能为空"
            if len(pattern) > max_len:
                return None, "", f"关键词太长了，最多 {max_len} 个字符"
            try:
                re.compile(pattern)
            except re.error as exc:
                return None, "", f"这个正则写错了：{exc}"
            return pattern, "regex", ""

        if len(raw) > max_len:
            return None, "", f"关键词太长了，最多 {max_len} 个字符"
        return raw, "text", ""

    @staticmethod
    def _display(item: dict[str, Any]) -> str:
        """把存储结构还原成用户看得懂的样子。"""
        keyword = item.get("kw", "")
        return f"/{keyword}/" if item.get("type") == "regex" else keyword

    def _subs_of_group(self, group_id: str) -> dict[str, Any]:
        return self._data["groups"].get(group_id) or {}

    def _keywords_of(self, group_id: str, user_id: str) -> list[dict[str, Any]]:
        sub = self._subs_of_group(group_id).get(user_id) or {}
        return sub.get("keywords") or []

    def _count_keywords(self) -> int:
        total = 0
        for users in self._data.get("groups", {}).values():
            for sub in users.values():
                total += len(sub.get("keywords") or [])
        return total

    async def _add_subscription(
        self, group_id: str, user_id: str, raw_keyword: str
    ) -> tuple[bool, str]:
        keyword, keyword_type, error = self._parse_keyword(raw_keyword)
        if keyword is None:
            return False, error

        max_count = max(1, self._int_conf("max_keywords_per_user", 20))

        async with self._lock:
            group_subs = self._data["groups"].setdefault(group_id, {})
            sub = group_subs.setdefault(user_id, {"keywords": []})
            items: list[dict[str, Any]] = sub.setdefault("keywords", [])

            display = f"/{keyword}/" if keyword_type == "regex" else keyword
            if any(
                item.get("kw") == keyword and item.get("type") == keyword_type
                for item in items
            ):
                return False, f"你已经订阅过「{display}」了"

            if len(items) >= max_count:
                return False, (
                    f"每个人最多订阅 {max_count} 个关键词，"
                    f"可以先删掉一些（/{CMD_LIST} 查看）"
                )

            items.append({"kw": keyword, "type": keyword_type, "ts": int(time.time())})
            await self._save()
            count = len(items)

        return True, f"已订阅「{display}」（{count}/{max_count}）"

    async def _remove_subscription(
        self, group_id: str, user_id: str, raw_keyword: str
    ) -> tuple[bool, str]:
        keyword, keyword_type, error = self._parse_keyword(raw_keyword)
        if keyword is None:
            return False, error

        display = f"/{keyword}/" if keyword_type == "regex" else keyword

        async with self._lock:
            group_subs = self._data["groups"].get(group_id) or {}
            sub = group_subs.get(user_id) or {}
            items: list[dict[str, Any]] = sub.get("keywords") or []

            remaining = [
                item
                for item in items
                if not (
                    item.get("kw") == keyword and item.get("type") == keyword_type
                )
            ]
            if len(remaining) == len(items):
                return False, f"没找到「{display}」，用 /{CMD_LIST} 看看你订阅了什么"

            sub["keywords"] = remaining
            if not remaining:
                group_subs.pop(user_id, None)
            if not group_subs:
                self._data["groups"].pop(group_id, None)

            await self._save()

        return True, f"已取消订阅「{display}」"

    async def _clear_subscriptions(self, group_id: str, user_id: str) -> int:
        async with self._lock:
            group_subs = self._data["groups"].get(group_id) or {}
            sub = group_subs.get(user_id) or {}
            count = len(sub.get("keywords") or [])
            if count:
                group_subs.pop(user_id, None)
                if not group_subs:
                    self._data["groups"].pop(group_id, None)
                await self._save()
            return count

    def _subscriptions_of_user(self, user_id: str) -> dict[str, list[dict[str, Any]]]:
        """按群汇总某个用户的全部订阅。"""
        result: dict[str, list[dict[str, Any]]] = {}
        for group_id, users in self._data.get("groups", {}).items():
            sub = users.get(user_id) or {}
            items = sub.get("keywords") or []
            if items:
                result[group_id] = items
        return result

    # ------------------------------------------------------------------
    # 发送辅助
    # ------------------------------------------------------------------
    def _get_bot(self, event: AstrMessageEvent | None = None) -> Any:
        if event is not None:
            bot = getattr(getattr(event, "platform", None), "bot", None)
            if bot is not None:
                return bot

        try:
            for instance in self.context.platform_manager.get_insts():
                meta = instance.meta()
                if getattr(meta, "name", "") == "aiocqhttp":
                    bot = getattr(instance, "bot", None)
                    if bot is not None:
                        return bot
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[keyword_watch] 获取 aiocqhttp 适配器失败: {exc}")
        return None

    async def _send_private(
        self, event: AstrMessageEvent, target_qq: str, content: str
    ) -> bool:
        """私聊发送，返回是否成功。优先走 OneBot 原生 API。"""
        if not target_qq.isdigit():
            logger.warning(f"[keyword_watch] 接收人不是合法 QQ 号: {target_qq}")
            return False

        bot = self._get_bot(event)
        if bot is not None:
            try:
                await bot.call_action(
                    action="send_private_msg",
                    user_id=int(target_qq),
                    message=[{"type": "text", "data": {"text": content}}],
                )
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[keyword_watch] 私聊 {target_qq} 失败: {exc}，尝试降级发送"
                )

        try:
            platform_id = event.get_platform_id() or "aiocqhttp"
            umo = f"{platform_id}:FriendMessage:{target_qq}"
            await self.context.send_message(umo, MessageChain().message(content))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[keyword_watch] 私聊 {target_qq} 最终失败: {exc}")
            return False

    # ------------------------------------------------------------------
    # 主流程：扫群消息
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """监听群消息，命中订阅关键词就私聊提醒订阅者。"""
        try:
            await self._scan_message(event)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[keyword_watch] 扫描群消息异常: {exc}", exc_info=True)

    async def _scan_message(self, event: AstrMessageEvent) -> None:
        if not self._bool_conf("enabled", True):
            return

        group_id = str(event.get_group_id() or "")
        if not group_id:
            return

        group_subs = self._subs_of_group(group_id)
        if not group_subs:
            return

        text = event.get_message_str() or ""
        if not text.strip():
            return

        sender_id = str(event.get_sender_id() or "")
        self_id = str(event.get_self_id() or "")
        if sender_id and sender_id == self_id:
            return

        # 指令消息默认不参与匹配，否则 /设置关键词提醒 紧急 会立刻提醒订阅「紧急」的人
        if self._bool_conf("skip_commands", True) and self._looks_like_command(event, text):
            return

        notify_self = self._bool_conf("notify_self", False)
        cooldown = max(0, self._int_conf("cooldown", 60))
        template = str(self.config.get("notify_template") or "") or DEFAULT_TEMPLATE

        # 群名顺手记进内存，写盘时一并持久化
        group_name = ""
        try:
            if event.message_obj.group is not None:
                group_name = event.message_obj.group.group_name or ""
        except Exception:  # noqa: BLE001
            group_name = ""
        if group_name and self._data.get("group_names", {}).get(group_id) != group_name:
            self._data.setdefault("group_names", {})[group_id] = group_name

        sender_name = event.get_sender_name() or sender_id
        now_str = self._now_str()
        now_ts = time.time()
        text_lower = text.lower()

        # 快照一份，避免 await 期间数据被其他协程改动
        for user_id, sub in list(group_subs.items()):
            if not notify_self and user_id == sender_id:
                continue

            hits: list[str] = []
            for item in list(sub.get("keywords") or []):
                keyword = item.get("kw") or ""
                if not keyword:
                    continue
                if item.get("type") == "regex":
                    pattern = self._compile_regex(keyword)
                    if pattern is not None and pattern.search(text):
                        hits.append(f"/{keyword}/")
                elif keyword.lower() in text_lower:
                    hits.append(keyword)

            if not hits:
                continue

            cooldown_key = (group_id, user_id)
            if cooldown > 0 and now_ts - self._last_notify.get(cooldown_key, 0) < cooldown:
                logger.info(
                    f"[keyword_watch] 用户 {user_id} 在冷却中，跳过本次通知"
                )
                continue
            self._last_notify[cooldown_key] = now_ts

            content = self._truncate(
                self._render(
                    template,
                    {
                        "text": text,
                        "sender": sender_name,
                        "sender_id": sender_id,
                        "group": group_id,
                        "group_name": group_name or group_id,
                        "time": now_str,
                        "keywords": "、".join(hits),
                        "keyword": hits[0],
                    },
                )
            )

            ok = await self._send_private(event, user_id, content)
            if ok:
                logger.info(
                    f"[keyword_watch] 群 {group_id} 命中「{'、'.join(hits)}」，"
                    f"已提醒 {user_id}"
                )
            else:
                logger.warning(
                    f"[keyword_watch] 群 {group_id} 提醒 {user_id} 失败（可能未加好友）"
                )

    def _looks_like_command(self, event: AstrMessageEvent, text: str) -> bool:
        """判断一条消息是不是在调用指令。

        AstrBot 在唤醒阶段可能已经把指令前缀剥掉了，只比较前缀会漏判，
        所以再直接比对一次指令名，避免 /设置关键词提醒 紧急 被当成普通聊天。
        """
        prefix = str(self.config.get("command_prefix") or "/") or "/"
        stripped = text.lstrip()
        if stripped.startswith(prefix):
            return True

        try:
            original = (getattr(event.message_obj, "message_str", "") or "").lstrip()
            if original and original.startswith(prefix):
                return True
        except Exception:  # noqa: BLE001 - 拿不到原始消息就跳过这层判断
            pass

        return any(stripped.startswith(name) for name in COMMAND_NAMES)

    def _compile_regex(self, pattern: str) -> re.Pattern[str] | None:
        if pattern in self._regex_cache:
            return self._regex_cache[pattern]
        try:
            compiled: re.Pattern[str] | None = re.compile(pattern)
        except re.error as exc:
            logger.error(f"[keyword_watch] 正则编译失败 [{pattern}]: {exc}")
            compiled = None
        self._regex_cache[pattern] = compiled
        return compiled

    # ------------------------------------------------------------------
    # 指令实现
    # ------------------------------------------------------------------
    @filter.command(CMD_ADD, alias={"添加关键词提醒"})
    async def cmd_add_keyword(self, event: AstrMessageEvent):
        """订阅一个关键词：/设置关键词提醒 紧急"""
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id() or "")

        if not group_id:
            yield event.plain_result(
                "这个指令要在群里发送，关键词只在设置它的那个群生效。"
            )
            return

        argument = self._extract_arg(event, CMD_ADD)
        if not argument:
            yield event.plain_result(f"用法：/{CMD_ADD} 关键词\n\n{HELP_TEXT}")
            return

        ok, message = await self._add_subscription(group_id, user_id, argument)

        if not ok:
            yield event.plain_result(f"❌ {message}")
            return

        # 私聊发一条确认，顺带验证私聊通道是否通
        dm_text = (
            f"✅ {message}\n"
            f"群：{self._group_label(group_id)}\n"
            f"命中后我会私聊提醒你。\n"
            f"查看全部订阅：/{CMD_LIST}"
        )
        dm_ok = False
        if self._bool_conf("send_confirm_dm", True):
            dm_ok = await self._send_private(event, user_id, dm_text)

        if self._bool_conf("reply_in_group", True) or not dm_ok:
            if self._bool_conf("send_confirm_dm", True) and not dm_ok:
                yield event.plain_result(
                    f"✅ {message}\n"
                    "但我没法私聊你，命中后收不到提醒。请先加我好友再试一次。"
                )
            else:
                yield event.plain_result(f"✅ {message}")

    @filter.command(CMD_LIST)
    async def cmd_list_keywords(self, event: AstrMessageEvent):
        """查看自己订阅的关键词：/我的关键词提醒"""
        user_id = str(event.get_sender_id() or "")
        subscriptions = self._subscriptions_of_user(user_id)

        if not subscriptions:
            yield event.plain_result(
                f"你还没有订阅任何关键词。\n"
                f"在群里发送 /{CMD_ADD} 关键词 就能订阅，例如：/{CMD_ADD} 紧急"
            )
            return

        current_group = str(event.get_group_id() or "")
        lines = ["【我的关键词提醒】"]
        total = 0
        for group_id, items in subscriptions.items():
            marker = "（当前群）" if group_id == current_group else ""
            lines.append(f"· {self._group_label(group_id)}{marker}")
            for index, item in enumerate(items, start=1):
                lines.append(f"   {index}. {self._display(item)}")
            total += len(items)
        lines.append(f"共 {total} 个，分布在 {len(subscriptions)} 个群。")
        lines.append(f"取消订阅：/{CMD_DEL} 关键词；清空本群：/{CMD_CLEAR}")

        content = "\n".join(lines)

        # 优先私聊，避免在群里刷屏；私聊不通再退回群消息
        if await self._send_private(event, user_id, content):
            if str(event.get_group_id() or ""):
                yield event.plain_result("已私聊发送你的关键词列表。")
        else:
            yield event.plain_result(content)

    @filter.command(CMD_DEL)
    async def cmd_delete_keyword(self, event: AstrMessageEvent):
        """取消订阅某个关键词：/删除关键词提醒 紧急"""
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id() or "")

        if not group_id:
            yield event.plain_result("这个指令要在群里发送。")
            return

        argument = self._extract_arg(event, CMD_DEL)
        if not argument:
            yield event.plain_result(
                f"用法：/{CMD_DEL} 关键词\n例如：/{CMD_DEL} 紧急"
            )
            return

        ok, message = await self._remove_subscription(group_id, user_id, argument)
        yield event.plain_result(("✅ " if ok else "❌ ") + message)

    @filter.command(CMD_CLEAR)
    async def cmd_clear_keywords(self, event: AstrMessageEvent):
        """清空自己在当前群的全部订阅：/清空关键词提醒"""
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id() or "")

        if not group_id:
            yield event.plain_result("这个指令要在群里发送。")
            return

        count = await self._clear_subscriptions(group_id, user_id)
        if count == 0:
            yield event.plain_result("你在本群没有订阅任何关键词。")
        else:
            yield event.plain_result(f"✅ 已清空你在本群的 {count} 个关键词订阅。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关键词提醒统计")
    async def cmd_stats(self, event: AstrMessageEvent):
        """管理员查看订阅概况。"""
        groups = self._data.get("groups", {})
        users = sum(len(group_users) for group_users in groups.values())
        lines = [
            "【关键词提醒统计】",
            f"群数量：{len(groups)}",
            f"订阅人数：{users}",
            f"关键词总数：{self._count_keywords()}",
            f"数据文件：{self._data_path}",
        ]
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 内部小工具
    # ------------------------------------------------------------------
    def _extract_arg(self, event: AstrMessageEvent, command: str) -> str:
        """从消息原文里取出指令后面的参数，保留正则里的空格。"""
        raw = (event.get_message_str() or "").strip()
        raw = re.sub(r"^[/!#]\s*", "", raw)

        if raw.startswith(command):
            argument = raw[len(command) :].strip()
        else:
            # 命中的是别名，退化成按第一个空白切分
            parts = raw.split(maxsplit=1)
            argument = parts[1].strip() if len(parts) > 1 else ""

        # /老板 在吗/ 这种带空格的正则，取到最后一个 / 为止
        if argument.startswith("/"):
            end = argument.rfind("/")
            if end > 0:
                return argument[: end + 1]
        return argument

    def _group_label(self, group_id: str) -> str:
        name = self._data.get("group_names", {}).get(group_id) or ""
        return f"「{name}」（{group_id}）" if name else f"群 {group_id}"

    async def terminate(self) -> None:
        """插件卸载/停用时清理内存状态。"""
        self._regex_cache.clear()
        self._last_notify.clear()
        logger.info("[keyword_watch] 插件已卸载")
