# 回归守则（humanoid_core）

这份文件记的是**反复犯过的毛病**：症状长什么样、根因在哪、哪条测试把它钉住了、
以后动到这里要注意什么。改代码前先扫一眼对应行；改完必须让「守门测试」保持绿。

> 为什么需要它：这个项目的历史是一串「同一个毛病换个地方回来」。例如「她的一天
> 高度雷同」被修过三次（prompt 留白、范例池、困意越线），每次都是删掉一个机制
> 或换掉一个默认值，而下一次改动又把它带回来。测试能挡住单点，守则能挡住方向。

| 症状 | 根因 | 守门测试 | 以后改这里要注意什么 |
|---|---|---|---|
| 困意 86 还在厨房煮饭；白天困到不行却不睡 | 越线阈值 88 太高（只有快撑不住才够得着）；prompt 只写「困到不行就去睡」，没把「白天也该选择休息」说清；本地兜底白天不安排小睡 | `test_v2280_schedule_dreams.py::PressureTriggerTest::test_due_fires_at_80`、`test_threshold_is_80`、`DaytimeNapFallbackTest::test_daytime_high_pressure_naps`；`test_v2280_schedule_dreams.py::SegmentPromptRulesTest::test_pressure_rule_from_body` | 阈值只有 **80** 这一条线：`CHANGE_TRIGGER_SLEEP_PRESSURE`、prompt 里的「困意已经 80+/100」、本地兜底的 82 门槛是三个不同用途（越线重排 / 告知 / 兜底小睡），不要为了「别太敏感」把 80 调回去。改阈值必须同时改 prompt 文案与测试。 |
| 夜猫子凌晨活动被砍；一过零点就被排睡觉 | 日程 prompt 没有「凌晨也是完整的一天」这条；也没从人设里识别夜猫子/倒班/白天补觉 | `test_v2280_schedule_dreams.py::SegmentPromptRulesTest::test_midnight_is_a_full_day`；`test_v2280_traits.py::DetectTest::test_night_owl_words`、`test_trait_hints_night_owl_mentions_midnight` | 凌晨不是天然睡眠时段，但**早起型的人凌晨在睡是正常的**——规则里两句话要一起出现。不要改回「按配置夜间窗口决定作息」（v2.16 已经证明那样会替所有角色决定几点睡）。 |
| 睡眠段没有任何可回忆的内容；社交层拿不到梦 | 睡眠段只有一个「睡眠」事件，`day` 契约只有 doing/done/next | `test_v2280_schedule_dreams.py::ParseSegmentDreamsTest`、`DreamPlumbingTest`（carry/migrate/day_phrases/status 全链路）、`test_v2280_contract.py::ContractDreamsAndWakeTest::test_day_dreams_and_wake_are_exported` | 梦只属于**睡眠段**（`is_sleep_event`）；`continue=true` 接着睡要继承上一段的梦；跨夜 carry 与跨天 migrate 都要带 `dreams`；读取一律走 `_clean_dreams()` 容错。`day_phrases()` 的键是社交侧契约，**空列表也必须给键**。 |
| 明明在睡觉却生龙活虎；被消息吵醒后立刻完全清醒 | 睡着时注入只有「这会儿在睡」；醒来没有任何惯性状态；消息吵到睡眠没有留下痕迹 | `test_v2280_sleep_inertia.py::NoteDisturbedTest`、`WakeInertiaTest`、`PromptSleepStateTest`、`MessageDisturbanceTest` | 消息**不掀被子**（睡着的那一段不重掷、不越线打断）是既有保护，不要为了「有反应」去改它；要加的是 `note_disturbed` 痕迹 + 醒来的惯性。`snapshot()` 的键是契约/测试锚点，新状态只能走 `wake_state()`。`show_sleep_window=False` 时睡眠事实整块不出。 |
| 「认识多久不进上下文」被当成规矩；用户要求加回认识天数 | 旧规则担心模型把天数拿去念「我们认识这么久了」，于是完全不给 | `test_v222_injection.py::test_days_reach_the_context`、`test_v2280_relation_days.py` | 这是**用户明确要求的有意反转**。注入里只给一句陈述事实，不写「所以你们应该…」这种引导；`_days_line()` 仍只给面板看，不要合并两条口径。 |
| 所有角色都像同一个勤快的模板人；夜猫子/懒散/宅的人设被无视 | 日程 prompt 默认「把一天排满」；过程服务完全不读人设；`ProcessService` 曾零实例化 | `test_v2280_traits.py::DetectTest`、`ProcessTemplateTraitTest`；`test_v2280_schedule_dreams.py::SegmentPromptRulesTest::test_diligence_is_not_the_default` | 特质识别**保守**：只认明确正向词，简单否定不算；勤快 × 懒散只输出一条线索；没有特质时必须与旧版逐字段一致（模板池原样、类别推断不动）。不要把「懒散」实现成罢工——这一段是日程定的。 |
| 基线里一直有 1 个 failed，看起来像功能坏了 | 配置面板底部那组隐藏开关的说明文案测试，仍在断言 description 等于一个短代号，而面板文案早就是一句完整中文说明 | 该开关测试里的 schema 断言（`tests/test_ccb.py` 的 `test_config_lands_last_in_schema`） | 陈旧断言按现实改，不是删测试；以后加配置项时，schema 与 `HumanoidConfig` 必须同时改（`test_schema.py` 盯着）。 |
| 社交层读不到新契约字段时整段坏掉 / Core 升级后旧存档报错 | 契约/门面新增字段没有做「只加不改 + 缺字段降级」 | `test_v2280_contract.py`（旧契约、旧日程、坏 soma 三种降级）、`test_link.py::ContractTest::test_contract_shape_is_stable`、`test_v22740_facade.py::BriefShapeTest` | `CONTRACT_VERSION` / `API_VERSION` 只在**破坏性**变化时动；新增能力只追加到 capabilities；读取方一律按「缺键 = 没有」处理。旧 slot 没有 `dreams`、旧 soma 没有 wake 字段、旧 carry 没有梦，都必须安全降级。 |
| 同一件事在注入里说两遍（「她在睡」+「正睡着」、「刚睡醒」+「刚醒」） | 新状态句与旧体感/余韵句各说各的 | `test_v2280_sleep_inertia.py::PromptSleepStateTest::test_inertia_line_replaces_the_generic_wake_echo`；`test_v2192_checkup.py::SleepSentenceTest::test_no_doubled_subject_when_asleep` | 插新句子前先确认有没有旧句在说同一件事；`_sleep_state_lines()` 在时不再保留体感里的「这会儿在睡」与余韵里的「刚睡醒」。一个事实只留一个来源。 |
| 日程 prompt 又变回「每天把一天排满」的风格 | 规则被删/被新规则稀释；范例池又被压成占位符 | `test_v2236_schedule_prompt.py`（范例、时长、衔接）、`test_v2280_schedule_dreams.py::SegmentPromptRulesTest` | 「怎么决定」只许追加、不许删除旧规则；新规则要写清「为什么」而不是口号。举例池按（角色+日期）抽，别改回写死的几条。 |
| 睡眠窗口/生物钟按配置固定窗口判，夜猫子被当成 23:00–06:00 | 曾经用配置窗口硬套所有人（v2.16 修过） | `test_link.py::ContractTest::test_contract_follows_a_night_owl_persona`、`test_v22733_rhythm.py` 身体/窗口相关用例 | 「现在是不是她的夜里」用 `soma.biological_night()` / `in_scheduled_night()`（日程推出来的），配置窗口只在没日程时兜底。 |
| 注入里出现体检单读数（`/100`、`%`、UTC 偏移）或给模型下命令 | 把参考数值/阈值直接写进事实块；新句子长了「应该/必须」 | `test_v2151_freedom.py`、`test_v216_facts.py` | 事实块只给状态，不给数字、不给指令。新增注入句先在脑子里过一遍：这句话是「她此刻是什么样」还是「模型该怎么做」？后者不写。 |
| 她主动找过这个人，注入里却说「你们有 N 天没说话了」；情绪分析看不到她主动开口 | Core 只通过 `on_message`（用户发来的消息）学「这段关系发生过什么」。主动消息是别的插件用 `context.send_message` 发的，**不经过 respond 阶段、不产生消息事件**，Core 永远看不到；`mood.spoke_log` 也只记主链路回复 | `tests/test_v2281_outgoing_spoke.py`（8 条：只记私聊、只收 LLM 类结果、不重复记账、推进 `last_interaction`、旧存档缺 `spoke_log` 不抛） | 官方 `after_message_sent` 钩子在 `event.send()` 之后、`event.clear_result()` 之前触发（`core/pipeline/respond/stage.py`），此时 `event.get_result()` 拿得到原文。只收 `LLM_RESULT` / `STREAMING_FINISH`；**只记私聊**（群里一句话可能是回复任何一个人的）；与 `note_her_reply` / `capture_reply_on_send` 共用 `note_spoke` 的 90 秒同文去重 |
| 普通聊天里「上下文里没有插件的任何痕迹」：她看不到自己困不困、饿不饿 | v2.27.38 把「〔她的身体与生活〕」标记块从每轮注入改成只在主动消息时注入（理由正当：那里面混着版本号/uid/块头），但没给普通聊天补替代品 | `tests/test_v2281_body_in_chat.py`（11 条） | 不要原样加回标记块。只取 `soma.feelings()` 的档位词，显著度 ≥0.75、最多三条、主语只出现一次，走同一条 `mark_as_temp()` 临时块。读数（`/100`）只属于 `/你的状态`；框架句一个字不动 |
| 她分不清对方是「刚说完」还是「快半小时了」：注入里的钟点只到分，两条消息隔几十秒时在她的时间里是同一时刻 | `_background_lines()` 那句 `现在` 用的是 `%H:%M`，而「此刻的她」是每轮重发的临时块，消息间隔常常只有几十秒 | `tests/test_v2281_time_seconds.py`（11 条：秒在、秒跟着钟走、秒进 state_note、秒不进事实块、时段词仍不出现、框架句不提「秒」） | 绝对钟点用 `%H:%M:%S`。「怕模型念出来」不靠删精度解决——「要不要提」由 `build_framing()` 的框架句管（「它们是背景，不是要拿出来讲的内容」），框架句一个字不动。真正要禁的是「上午/下午/晚上」这类时段词，它们才会被当成打招呼由头 |
| 坏存档 / 手改 state.json 让插件崩或静默错值 | 读路径假设字段形状；`bool("false")` 当真 | `test_v2280_sleep_inertia.py::WakeInertiaTest::test_wake_state_survives_old_and_bad_state`；`test_v22733_rhythm.py::ProcessDirtyStateTest`；`test_state_and_bad_values.py` | 状态文件是用户能手改的：读路径一律容错，坏值按默认值处理；不要用 `bool(str)` 解析布尔。 |

## 「现实→网友」/「变成手机发消息」是什么（v1.28.1 / v2.28.1 调查结论）

**不是插件的问题，也无需修。** 已按用户反馈（3231002996，昵称「鲨白」）逐条核对过日志。

### 数据

该用户 240 条 event_bus 记录：

| 类型 | 条数 | 昵称显示 |
|---|---|---|
| 真正的文字消息 | 39 | 正常显示「鲨白」 |
| 戳一戳 / 图片 / 表情 / 语音 / 空 | 201 | 显示成 QQ 号 |

### 官方代码路径

`astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py:211-214`（AstrBot 4.29.1 官方原文）：

    abm.sender = MessageMember(
        str(event.sender["user_id"]),
        event.sender.get("card") or event.sender.get("nickname", "N/A"),
    )

昵称只从 OneBot v11 事件的 `sender.card` / `sender.nickname` 取。而 **OneBot v11 规范里，
`notice` 类事件（戳一戳）和纯图片/表情/语音消息的 `sender` 字段本来就是空的或不完整**，
于是 AstrBot 只能回退显示 QQ 号。

695 个发件人里昵称含「关系/分组词」的只有 1 个：`开朗的网友/2301401892`，是对方自起的昵称。
没有任何插件调用过 `set_friend_category`（ccb_plus 只读 `get_stranger_info`；
relationship 的 `delete_friend`/`set_group_leave` 是删好友/退群，与分组无关且需命令触发；
user_tag 的「网友」是它自己关系库的 DEPRECATED 项，完全不碰 OneBot API）。

### 「突然变成手机发消息」

QQ 的「手机/Pad/PC」由**当前在线会话**决定。NapCat 用平板协议登录，掉线重连时若手机 QQ
恰好在线抢到主会话，对方那边显示就切回手机端。与上下文压缩无关（压缩只影响模型 prompt，
不影响 QQ 设备显示），是时间上的巧合。日志时间线：

    00:15:13  [ERRO] Exception on websocket /ws
    00:15:14  [INFO] aiocqhttp(OneBot v11) 适配器已连接
    00:20:26  [INFO] [连接已中断]
    00:21:48  [WARN] [阿莉娜] OneBot HTTP 3002 不通 (第1/15次)
    00:22:52  [RECOVER] [阿莉娜] 已恢复正常

### 处置

无需改插件。要减少观感只能从 NapCat 侧降低掉线频率（网络稳定性/心跳/避免多端抢占）。


## 改完自检

1. 在 `work/core` 下跑全量测试，必须 0 failed（允许既有 1 skipped）；
2. 新增行为至少有一条测试会在**旧代码**上变红；
3. 如果改的是语义反转（如认识天数），在 CHANGELOG 里写明「这是用户要求的反转」，
   并列清改了哪条既有断言、为什么；
4. 改了契约/门面字段：只加不改，同步 capabilities，补旧数据降级测试；
5. 改了睡眠/日程主干：先确认滚动分段、跨夜 carry、睡眠保护三件事没有被顺手改掉。
