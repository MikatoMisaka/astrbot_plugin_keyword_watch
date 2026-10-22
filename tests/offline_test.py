"""不依赖真实 AstrBot 的离线测试：mock 掉 astrbot 模块后跑关键词订阅插件。"""

import asyncio
import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(tempfile.mkdtemp(prefix="kw_watch_test_"))
# 当前生效的数据目录，每个用例切一份，避免互相污染
CURRENT_DATA_DIR = [DATA_ROOT]


def use_fresh_dir() -> Path:
    path = Path(tempfile.mkdtemp(dir=DATA_ROOT))
    CURRENT_DATA_DIR[0] = path
    return path


def build_fake_astrbot():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")

    class _Logger:
        def __init__(self):
            self.records = []

        def _log(self, level, msg):
            self.records.append((level, msg))

        def info(self, msg, *a, **k):
            self._log("INFO", msg)

        def warning(self, msg, *a, **k):
            self._log("WARN", msg)

        def error(self, msg, *a, **k):
            self._log("ERROR", msg)

        def exception(self, msg, *a, **k):
            self._log("EXC", msg)

    logger = _Logger()
    api.logger = logger
    api.AstrBotConfig = dict

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:  # noqa: D101
        pass

    class MessageChain:  # noqa: D101
        def __init__(self):
            self.parts = []

        def message(self, text):
            self.parts.append(text)
            return self

    class _Deco:
        def __call__(self, func):
            return func

    class _Filter:
        EventMessageType = types.SimpleNamespace(
            ALL="all", GROUP_MESSAGE="group", PRIVATE_MESSAGE="private"
        )
        PermissionType = types.SimpleNamespace(ADMIN="admin")

        @staticmethod
        def command(name, alias=None, **kw):
            return _Deco()

        @staticmethod
        def command_group(name):
            return _Deco()

        @staticmethod
        def event_message_type(t):
            return _Deco()

        @staticmethod
        def permission_type(t):
            return _Deco()

    event_mod.filter = _Filter()
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain

    star_mod = types.ModuleType("astrbot.api.star")

    class Context:  # noqa: D101
        def __init__(self):
            self.sent = []
            self.platform_manager = types.SimpleNamespace(get_insts=lambda: [])

        async def send_message(self, umo, chain):
            self.sent.append((umo, "".join(chain.parts)))

    class Star:  # noqa: D101
        def __init__(self, context):
            self.context = context

    class StarTools:  # noqa: D101
        @classmethod
        def get_data_dir(cls, plugin_name=None):
            return Path(CURRENT_DATA_DIR[0])

    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.StarTools = StarTools

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
    return logger


class FakeEvent:
    def __init__(self, text, group_id="123456", sender_id="111", sender_name="张三",
                 group_name="摸鱼群"):
        self._text = text
        self._group_id = group_id
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._group_name = group_name
        self.message_obj = types.SimpleNamespace(
            group=types.SimpleNamespace(group_name=group_name)
            if group_id
            else None
        )
        self.platform = None

    def get_group_id(self):
        return self._group_id

    def get_message_str(self):
        return self._text

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_self_id(self):
        return "999"

    def get_platform_id(self):
        return "qq_bot"

    def plain_result(self, text):
        return ("plain", text)


def load_plugin():
    logger = build_fake_astrbot()
    spec = importlib.util.spec_from_file_location(
        "kw_watch_main", PLUGIN_DIR / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, logger


async def collect(agen):
    """把 async generator 的 yield 结果收集起来。"""
    out = []
    async for item in agen:
        out.append(item)
    return out


async def main():
    module, logger = load_plugin()
    Plugin = module.KeywordWatchPlugin
    results = []

    def check(name, cond, extra=""):
        results.append((name, bool(cond), extra))

    async def new_plugin(cfg=None, ctx=None, fresh=True):
        if fresh:
            use_fresh_dir()
        ctx = ctx or module.Context()
        return Plugin(ctx, cfg or {}), ctx

    # ---------- 指令：添加 ----------
    p, ctx = await new_plugin()
    out = await collect(p.cmd_add_keyword(FakeEvent("/设置关键词提醒 紧急")))
    check("添加关键词有回执", len(out) == 1, str(out))
    check("添加后私聊确认", len(ctx.sent) == 1, f"{len(ctx.sent)} 条")
    check("订阅数据已写入", len(p._keywords_of("123456", "111")) == 1)

    # 重复添加
    out = await collect(p.cmd_add_keyword(FakeEvent("/设置关键词提醒 紧急")))
    check("重复添加被拒", "已经订阅过" in out[0][1], out[0][1])

    # 无参数
    out = await collect(p.cmd_add_keyword(FakeEvent("/设置关键词提醒")))
    check("无参数提示用法", "用法" in out[0][1], out[0][1])

    # 空关键词
    out = await collect(p.cmd_add_keyword(FakeEvent("/设置关键词提醒")))
    check("无参数不写入", len(p._keywords_of("123456", "111")) == 1)

    # 私聊里发（无群上下文）
    out = await collect(p.cmd_add_keyword(FakeEvent("/设置关键词提醒 紧急", group_id="")))
    check("私聊设置被拒", "要在群里发送" in out[0][1], out[0][1])

    # ---------- 正则关键词 ----------
    p2, ctx2 = await new_plugin()
    out = await collect(p2.cmd_add_keyword(FakeEvent(r"/设置关键词提醒 /订单\d+/")))
    check("正则关键词可添加", len(p2._keywords_of("123456", "111")) == 1, out[0][1])
    check("正则类型识别正确", p2._keywords_of("123456", "111")[0]["type"] == "regex")

    out = await collect(p2.cmd_add_keyword(FakeEvent("/设置关键词提醒 /([bad/")))
    check("错误正则有提示", "正则写错了" in out[0][1], out[0][1])
    check("错误正则不入库", len(p2._keywords_of("123456", "111")) == 1)

    # 正则里带空格
    out = await collect(p2.cmd_add_keyword(FakeEvent("/设置关键词提醒 /老板 在吗/")))
    kws = [i["kw"] for i in p2._keywords_of("123456", "111")]
    check("带空格的正则完整保留", "老板 在吗" in kws, str(kws))

    # ---------- 长度与数量上限 ----------
    p3, _ = await new_plugin({"max_keyword_length": 5})
    out = await collect(p3.cmd_add_keyword(FakeEvent("/设置关键词提醒 这个关键词太长了")))
    check("超长关键词被拒", "太长" in out[0][1], out[0][1])

    p4, _ = await new_plugin({"max_keywords_per_user": 2})
    await collect(p4.cmd_add_keyword(FakeEvent("/设置关键词提醒 一")))
    await collect(p4.cmd_add_keyword(FakeEvent("/设置关键词提醒 二")))
    out = await collect(p4.cmd_add_keyword(FakeEvent("/设置关键词提醒 三")))
    check("超过数量上限被拒", "最多订阅" in out[0][1], out[0][1])
    check("上限内已存入两个", len(p4._keywords_of("123456", "111")) == 2)

    # ---------- 命中通知 ----------
    p5, ctx5 = await new_plugin()
    # 用户 111 订阅，用户 222 未订阅
    await collect(p5.cmd_add_keyword(FakeEvent("/设置关键词提醒 紧急", sender_id="111")))
    ctx5.sent.clear()

    # 别人在群里说到「紧急」
    await p5.on_group_message(FakeEvent("老板：有紧急情况，速回", sender_id="222"))
    check("命中后提醒订阅者", len(ctx5.sent) == 1, f"{len(ctx5.sent)} 条")
    if ctx5.sent:
        umo, text = ctx5.sent[0]
        check("提醒发给订阅者本人", umo == "qq_bot:FriendMessage:111", umo)
        check("提醒含原文", "有紧急情况" in text, text)
        check("提醒含命中的词", "紧急" in text, text)

    # 未命中
    ctx5.sent.clear()
    await p5.on_group_message(FakeEvent("今天天气不错", sender_id="222"))
    check("未命中不提醒", len(ctx5.sent) == 0)

    # 自己发的消息不提醒自己
    ctx5.sent.clear()
    await p5.on_group_message(FakeEvent("紧急！", sender_id="111"))
    check("自己发的消息不提醒自己", len(ctx5.sent) == 0)

    # 打开 notify_self 后提醒自己
    p5b, ctx5b = await new_plugin({"notify_self": True})
    await collect(p5b.cmd_add_keyword(FakeEvent("/设置关键词提醒 紧急", sender_id="111")))
    ctx5b.sent.clear()
    await p5b.on_group_message(FakeEvent("紧急！", sender_id="111"))
    check("notify_self 开启后提醒自己", len(ctx5b.sent) == 1)

    # 指令消息被跳过
    ctx5.sent.clear()
    await p5.on_group_message(FakeEvent("/设置关键词提醒 紧急", sender_id="222"))
    check("指令消息不触发关键词", len(ctx5.sent) == 0)

    # 关闭跳过后指令也会触发
    p6, ctx6 = await new_plugin({"skip_commands": False})
    await collect(p6.cmd_add_keyword(FakeEvent("/设置关键词提醒 紧急", sender_id="111")))
    ctx6.sent.clear()
    await p6.on_group_message(FakeEvent("/紧急 帮助一下", sender_id="222"))
    check("关闭跳过后指令可触发", len(ctx6.sent) == 1)

    # ---------- 跨群隔离 ----------
    p7, ctx7 = await new_plugin()
    # 在 123456 群订阅
    await collect(p7.cmd_add_keyword(FakeEvent("/设置关键词提醒 紧急", group_id="123456", sender_id="111")))
    ctx7.sent.clear()
    # 另一个群有人提到「紧急」
    await p7.on_group_message(FakeEvent("紧急情况", group_id="888888", sender_id="222"))
    check("关键词不跨群生效", len(ctx7.sent) == 0)
    # 原群命中
    await p7.on_group_message(FakeEvent("紧急情况", group_id="123456", sender_id="222"))
    check("原群正常提醒", len(ctx7.sent) == 1)

    # ---------- 一条消息命中多个词只发一条 ----------
    p8, ctx8 = await new_plugin()
    await collect(p8.cmd_add_keyword(FakeEvent("/设置关键词提醒 紧急", sender_id="111")))
    await collect(p8.cmd_add_keyword(FakeEvent("/设置关键词提醒 上线", sender_id="111")))
    ctx8.sent.clear()
    await p8.on_group_message(FakeEvent("紧急，马上上线", sender_id="222"))
    check("多词命中只发一条", len(ctx8.sent) == 1, f"{len(ctx8.sent)} 条")
    if ctx8.sent:
        check("提醒里列出全部命中词", "紧急" in ctx8.sent[0][1] and "上线" in ctx8.sent[0][1])

    # ---------- 冷却 ----------
    p9, ctx9 = await new_plugin({"cooldown": 60})
    await collect(p9.cmd_add_keyword(FakeEvent("/设置关键词提醒 紧急", sender_id="111")))
    ctx9.sent.clear()
    await p9.on_group_message(FakeEvent("紧急一", sender_id="222"))
    await p9.on_group_message(FakeEvent("紧急二", sender_id="222"))
    check("冷却期内只提醒一次", len(ctx9.sent) == 1, f"{len(ctx9.sent)} 条")

    # cooldown 0 时不限
    p10, ctx10 = await new_plugin({"cooldown": 0})
    await collect(p10.cmd_add_keyword(FakeEvent("/设置关键词提醒 紧急", sender_id="111")))
    ctx10.sent.clear()
    await p10.on_group_message(FakeEvent("紧急一", sender_id="222"))
    await p10.on_group_message(FakeEvent("紧急二", sender_id="222"))
    check("冷却为0时每条都提醒", len(ctx10.sent) == 2, f"{len(ctx10.sent)} 条")

    # ---------- 删除 / 清空 ----------
    p11, _ = await new_plugin()
    await collect(p11.cmd_add_keyword(FakeEvent("/设置关键词提醒 紧急")))
    await collect(p11.cmd_add_keyword(FakeEvent("/设置关键词提醒 上线")))
    out = await collect(p11.cmd_delete_keyword(FakeEvent("/删除关键词提醒 紧急")))
    check("删除成功有回执", "已取消订阅" in out[0][1], out[0][1])
    check("删除后只剩一个", len(p11._keywords_of("123456", "111")) == 1)
    out = await collect(p11.cmd_delete_keyword(FakeEvent("/删除关键词提醒 不存在的词")))
    check("删除不存在的词有提示", "没找到" in out[0][1], out[0][1])

    out = await collect(p11.cmd_clear_keywords(FakeEvent("/清空关键词提醒")))
    check("清空有回执", "已清空" in out[0][1], out[0][1])
    check("清空后为空", len(p11._keywords_of("123456", "111")) == 0)
    out = await collect(p11.cmd_clear_keywords(FakeEvent("/清空关键词提醒")))
    check("重复清空有提示", "没有订阅" in out[0][1], out[0][1])

    # ---------- 查看列表 ----------
    p12, ctx12 = await new_plugin()
    out = await collect(p12.cmd_list_keywords(FakeEvent("/我的关键词提醒")))
    check("无订阅时给出引导", "还没有订阅" in out[0][1], out[0][1])

    await collect(p12.cmd_add_keyword(FakeEvent("/设置关键词提醒 紧急", group_id="123456", sender_id="111")))
    await collect(p12.cmd_add_keyword(FakeEvent("/设置关键词提醒 bug", group_id="777777", sender_id="111")))
    ctx12.sent.clear()
    out = await collect(p12.cmd_list_keywords(FakeEvent("/我的关键词提醒", group_id="123456", sender_id="111")))
    check("列表优先私聊发送", len(ctx12.sent) == 1, f"{len(ctx12.sent)} 条")
    if ctx12.sent:
        text = ctx12.sent[0][1]
        check("列表含两个群", "123456" in text and "777777" in text, text)
        check("列表标注当前群", "当前群" in text, text)

    # ---------- 持久化 ----------
    saved_path = CURRENT_DATA_DIR[0] / "subscriptions.json"
    check("数据文件已生成", saved_path.exists(), str(saved_path))
    if saved_path.exists():
        raw = json.loads(saved_path.read_text(encoding="utf-8"))
        check("文件结构正确", "groups" in raw and "123456" in raw["groups"], str(raw)[:120])

    # 用同一份数据文件重新实例化，模拟重启
    p13, ctx13 = await new_plugin(fresh=False)
    reloaded = p13._subscriptions_of_user("111")
    check("重启后订阅仍在", len(reloaded) >= 2, str(list(reloaded.keys())))
    ctx13.sent.clear()
    await p13.on_group_message(FakeEvent("紧急情况", group_id="123456", sender_id="222"))
    check("重启后仍能命中提醒", len(ctx13.sent) == 1)

    # ---------- 坏数据文件自愈 ----------
    broken_dir = use_fresh_dir()
    broken = broken_dir / "subscriptions.json"
    broken.write_text("{ this is not json", encoding="utf-8")
    p14, _ = await new_plugin(fresh=False)
    check("坏文件不导致崩溃", isinstance(p14._data, dict))
    check("坏文件被重置为空", len(p14._data.get("groups", {})) == 0)
    backups = list(broken_dir.glob("subscriptions.broken.*.json"))
    check("坏文件有备份", len(backups) >= 1, str(backups))

    # ---------- 前缀被框架剥离的情况 ----------
    p16, ctx16 = await new_plugin({"skip_commands": True})
    await collect(p16.cmd_add_keyword(FakeEvent("/设置关键词提醒 紧急", sender_id="111")))
    ctx16.sent.clear()
    # 模拟 AstrBot 已把 "/" 前缀剥掉，只留指令名
    await p16.on_group_message(FakeEvent("设置关键词提醒 紧急", sender_id="222"))
    check("前缀被剥离时仍跳过指令", len(ctx16.sent) == 0, f"{len(ctx16.sent)} 条")
    # 同样前缀被剥离的普通消息仍应命中
    await p16.on_group_message(FakeEvent("真的紧急了", sender_id="222"))
    check("剥离前缀不影响普通消息", len(ctx16.sent) == 1, f"{len(ctx16.sent)} 条")

    # ---------- 参数解析 ----------
    p15, _ = await new_plugin()
    check(
        "参数解析（主名）",
        p15._extract_arg(FakeEvent("/设置关键词提醒 紧急"), "设置关键词提醒") == "紧急",
        p15._extract_arg(FakeEvent("/设置关键词提醒 紧急"), "设置关键词提醒"),
    )
    check(
        "参数解析（无空格）",
        p15._extract_arg(FakeEvent("/设置关键词提醒紧急"), "设置关键词提醒") == "紧急",
        p15._extract_arg(FakeEvent("/设置关键词提醒紧急"), "设置关键词提醒"),
    )
    check(
        "参数解析（别名）",
        p15._extract_arg(FakeEvent("/添加关键词提醒 紧急"), "设置关键词提醒") == "紧急",
        p15._extract_arg(FakeEvent("/添加关键词提醒 紧急"), "设置关键词提醒"),
    )
    check(
        "参数解析（正则含空格）",
        p15._extract_arg(FakeEvent("/设置关键词提醒 /老板 在吗/"), "设置关键词提醒")
        == "/老板 在吗/",
        p15._extract_arg(FakeEvent("/设置关键词提醒 /老板 在吗/"), "设置关键词提醒"),
    )

    # ---------- 输出 ----------
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'=' * 62}\n测试结果：{passed}/{len(results)} 通过\n{'=' * 62}")
    for name, ok, extra in results:
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f"  << {extra}" if not ok else ""))

    errors = [r for r in logger.records if r[0] in ("ERROR", "EXC", "WARN")]
    if errors:
        print("\n插件告警/错误日志：")
        for level, msg in errors[:15]:
            print(f"  [{level}] {msg}")

    return passed == len(results)


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
