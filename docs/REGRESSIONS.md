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
| 她主动找过这个人，注入里却说「你们有 N 天没说话了」；情绪分析看不到她主动开口 | Core 只通过 `on_message`（用户发来的消息）学「这段关系发生过什么」。主动消息是别的插件用 `context.send_message` 发的，**不经过 respond 阶段、不产生消息事件**，Core 收不到任何事件。v2.28.1 曾用 `after_message_sent` 钩子去接——那个钩子只在 respond 阶段触发，接不到它；v2.28.4 已把钩子整块删除，改走信号桥 | `tests/test_v2282_signal_outgoing.py`（7 条：原文进 `spoke_log`、`last_interaction` 推到发送时刻、不倒退、不重记、旧插件缺字段降级、soma 关闭照记、单 Bot 顶层信号） | **别再用 `after_message_sent` 接主动消息**：`OnAfterMessageSentEvent` 只在 `core/pipeline/respond/stage.py:349` 触发，而 `StarContext.send_message` → `platform.send_by_session` 不经过那里。唯一通路是 `humanoid_signals.json` 的 `last_proactive_text`。Core 侧 `_note_social_outgoing()` 只在该时间戳前进时记一次；`last_interaction` 只往后推，不覆盖用户后来的发言；原文缺失就不记，不拿占位文本污染 `spoke_log` |
| 普通聊天里「上下文里没有插件的任何痕迹」：她看不到自己困不困、饿不饿 | **这个前提本身是错的**：每轮 `on_llm_request` 都会把 `core.build_injection()` 整块作为临时内容块追加，那块里一直有 `_feelings_lines()`（medium 2 条 / full 5 条，阈值 0.75）。v2.28.1 据此又在 `_state_note()` 里拼了一遍，于是同一句话在一轮请求里出现两次；v2.28.4 已把那整段删掉 | `tests/test_v2281_body_in_chat.py`（7 条：身体事实进得了 `build_injection`、`state_note` 不重复、整轮只出现一次、关掉生理层没有、身体层炸了不带走 state_note、框架句未放宽） | 身体事实**只有一个来源**：`build_injection()` 的〔她的身体与生活〕块。`_state_note()` 只放「此刻的她」（关系/心境/日程/外界/穿搭）。觉得「上下文里没有痕迹」时先查 `inject_activity_context` 档位和日志——**不要**在第二个地方再拼一遍 `soma.feelings()`，两个临时块会同时进同一轮请求 |
| 她分不清对方是「刚说完」还是「快半小时了」：注入里的钟点只到分，两条消息隔几十秒时在她的时间里是同一时刻 | `_background_lines()` 那句 `现在` 用的是 `%H:%M`，而「此刻的她」是每轮重发的临时块，消息间隔常常只有几十秒 | `tests/test_v2281_time_seconds.py`（11 条：秒在、秒跟着钟走、秒进 state_note、秒不进事实块、时段词仍不出现、框架句不提「秒」） | 绝对钟点用 `%H:%M:%S`。「怕模型念出来」不靠删精度解决——「要不要提」由 `build_framing()` 的框架句管（「它们是背景，不是要拿出来讲的内容」），框架句一个字不动。真正要禁的是「上午/下午/晚上」这类时段词，它们才会被当成打招呼由头 |
| 坏存档 / 手改 state.json 让插件崩或静默错值 | 读路径假设字段形状；`bool("false")` 当真 | `test_v2280_sleep_inertia.py::WakeInertiaTest::test_wake_state_survives_old_and_bad_state`；`test_v22733_rhythm.py::ProcessDirtyStateTest`；`test_state_and_bad_values.py` | 状态文件是用户能手改的：读路径一律容错，坏值按默认值处理；不要用 `bool(str)` 解析布尔。 |
| 她被人骂了一句，主动开口时却在说天气；社交层完全不知道她心里正过着一件事 | 事件机制（v2.28.5 起）只在 `schedule` 内部，**没出契约**。而社交层读的是契约里的 `ongoing`/`wants`/`dreams`——它拿到的「她今天在做什么」里没有任何一项是「她刚经历了什么」 | `tests/test_v2290_contract_event.py`（9 条：没事件时契约块为空、有事件时两键齐全、导出只读不取走、age 会走、过期与消费后为空、乱 kind 永不出现、capabilities 里有 event、API_VERSION 没动）；跨插件侧 `mod-social/tests/test_v1285_recent_event.py`（12 条） | 契约字段**只加不改**：`event` 是新增键，`API_VERSION`/`CONTRACT_VERSION` 不动，社交层按 `capabilities` 判断走哪条路。导出必须走 `schedule.pending_event()`——**只读**，用 `_consume_event()` 会让这一轮的决定再也拿不到这件事。社交侧两处都要接：`anchors._derive_from_state` 读**顶层** `body["event"]`，`groupflow` 读 `day["event"]`，`core_bridge` 因此两份都给（同一个 dict 对象，不会不一致）。旧 Core 没有这个字段、或手改坏成字符串/数字/列表/空 phrase → 一律空 dict，**不凭空多出一件没发生过的事** |
| 她的一天只被身体推动，用户的话进不去日程：骂她一句、夸她一句，她正做着的事照走 | `note_interruption()` 是个**计数器**——只回答「刚才有没有人来过」，不回答「她经历了什么」。三道闸（安静间隔 45 分钟 / 冷却 1 小时 / 只看私聊）把真实对话全挡在外面，而 `on_message` 拿到的 `text` 根本没往下传 | `tests/test_v2290_events.py`（20 条：五个 kind 都能翻出话、同一天稳定不同天不同、冷却与保鲜期、一次性消费、被闸门挡回原样还回、群消息也记、`refresh_due` 能被事件触发、极性五条） | **极性不重复判**：kind 由 mood 层账本给（`_event_kind`），日程层只转交（`core_instance._note_schedule_event`）。在日程层再判一遍就会出现「两个情绪层各说各话」——插件说她被夸了、日程说她被骂了。**中性的话不进日程**（`abs(weight) < 1e-3` 直接返回），否则问句和客套也值得她重排，多用户部署里她整天在改主意。**群消息也记**：以前 `is_group=True` 直接 return，群里发生的事她完全不知道。真正的闸在日程层：保鲜期 `EVENT_TTL_SECONDS`（30 分钟）+ 冷却 `EVENT_COOLDOWN_SECONDS`（5 分钟），被连骂三句只算一件事。事件**不绕过**闸门/频率/预算，只是多一条进入 `refresh_due` 的路；睡着的那段已经在 `_due` 前面 return 了——被夸也不掀被子 |
| 一句普通闲聊让「亲密次数」+1、好感 +5，「刚发生过」的冷却还被推到 5 小时后 | 词表里有裸的「做了」「进来」「要我」，而对象词表里有「你」「我」——「你做了什么」「你进来吧」「你要我怎么说」全都命中；同时 `note_occurrence` 写的是 `ccb_last_at`，而它正是 `evaluate` 的冷却锚点，一个键两种语义 | `tests/test_v2286_bilateral.py::FalsePositives`（谈论 vs 发生）、`KeySplit`（三个时间键互不覆盖）、`HerSide`（她的话只在场景里计数）、`GroupSilence`（群里不记账） | 记录层与状态机**是两条路，闸必须各有一份**：`evaluate` 的 `is_group` 硬闸挡不住 `on_message` 的记账，群里那句「你做了什么」原来照样改好感。三个时间键语义不同、不许合并：`ccb_last_at` = 上次发生（给人看）、`ccb_gate_at` = 冷却锚点（只在真进场景时写）、`ccb_event_dedup_at` = 一次事件只记一次（用户与她的话共用）。「在说这件事」≠「这件事发生了」：疑问/转述/打算用 `_CCB_TALK_GUARD` 挡在动作词**前面三个字**内，别放进整窗（那会误杀「我要你了」） |
| 她先说出口的亲密、她主动推进的，`ccb_done_count` 一条都不算；她说「住手」场景照走 | 记账只有**一个入口**：`core.on_message`（用户说的话）。`note_her_reply` / `capture_reply_on_send` 只调 `note_spoke`，她的正文从未进过 `detect_occurrence`；`detect_refusal` 也只看用户那条 | `tests/test_v2286_bilateral.py::HerSide::test_her_words_count_inside_a_scene`、`test_her_words_are_ignored_outside_a_scene`、`test_her_refusal_ends_the_scene_and_is_remembered`、`test_her_playful_word_is_not_a_refusal` | 她那一侧必须有两道闸：**只在 `ccb_stage > 0` 时记**（她的回复是从注入里长出来的，不设闸就是自证——模型复述一句内心状态就能刷计数器），以及**拒绝用严格词表**（`detect_refusal_hard`：宽词表里的「讨厌」在她嘴里多半是撒娇，判错就是结束场景 + 30 分钟静默）。她拒绝 ≠ 没记录，是「不发生」：写 `ccb_refused_at` + `reset_scene`，不计数 |
| 社交层从来看不到「有没有和对方亲密过」 | `CHANGELOG` v2.27.13 声称「契约快照多导出亲密次数/上次时间/上头值」，`link.py` 里一个都没有；契约的 `paths` 只有好感/称呼/上次互动/说过的话 | `tests/test_v2286_bilateral.py::Facade`（未知角色空、无记录给确定空、读不建壳、真值、`want_line` 受开关管） | 补的是**门面**不是契约：契约是角色级快照，塞 per-user 数据会让 state.json 随用户数线性涨（1000 人时已经 2.4 MB）。门面按需现算、走 `peek_user` 不建空壳、失败返回 `{}`。历史数据（次数/上次）与「她这会儿想不想」（`want_line`）分开授权——后者会变成一条主动消息，只有 `ccb_proactive` 打开时才给 |
| 亲密场景里「她换一轮就忘了自己什么姿势」，深度描写前后矛盾 | 状态里只有 `ccb_stage` / `ccb_satisfy`，没有姿势、没有位置；深度是按阶段估的**当前值**，阶段一回退就出现「刚才撑到疼、现在还没到底」 | `tests/test_v2286_scene.py::Judgement`（命中/否定/歧义）、`Injection`（她的正文也要驱动状态）、`Depth::test_peak_depth_is_monotonic` | 姿势/位置**必须双向扫描**：绝大多数是她在正文里写出来的，只扫用户那句会漏掉大半。判不出就保持原值，一次都判不出就整段不出（兜底不报错）。**判错比不知道更出戏**——歧义就返回"不知道"，绝不猜。否定护栏只看命中词**前面** 4 个字（「别停」里的"停"在后面，那是鼓励）。深度用**这一场到过的最大值**，单调不减 |
| 场景挂着过夜：第二天她还在「场景里」，没人能把它收掉 | `ccb_stage` 只有"数值掉下阈值"一个收场条件；到顶时旧代码只扣体力、不收场；也没有任何手动出口 | `tests/test_v2286_scene.py::Aftercare::test_peak_ends_the_scene_and_opens_the_window`、`Exit`（脱离 / 闲置 6 小时 / 精力见底） | 收场现在有三条路：到顶、闲置超时、精力见底，外加 `/拟人 脱离` 手动。**注意别把这三条并进 `scene_lapsed`**——那个函数的语义是"门槛真的掉下去了"，它决定了要不要清场景状态；冷却/封顶/群聊仍然不算结束（v2.27.17 的教训） |
| 把年龄接进性能力数值；或安全判断写宽了把人设全打掉 | 想用"看年龄就固定深度"这类推导；或把「哥哥」「妹妹」这类单独称谓当成血缘信号 | `tests/test_v2286_scene.py::Safety`（未成年词/年龄/血缘/**「哥哥」不误伤**、未成年人设连账都不记） | 年龄**只回答"是否成年"**，数值只来自静态配置。血缘只认**指向配对**的词（亲兄妹/父女/母子/乱伦），单称不算。安全闸排在 `evaluate` 最前面，且注入与记账两条路各有自己的一份闸——只在状态机拦，账上会留下「发生过 N 次而场景一次没出」 |
| 聊着天气突然冒出「约11cm/14cm·临界」——场景进了，可对话根本不在那儿 | 进门判断只看**数值**（亲近欲/好感够不够），从来不问"这一段在做什么"。而数值是常驻的，过线就进 | `tests/test_v2286_scene.py::SceneGate`（分数高/低、未知退回兜底、续场不拦）、`SceneScorePlumbing`（拿不到写 -1 不是 0、旧判断不能赶人、低分结束场景）；`tests/test_ccb.py::EndToEnd` 的 `_mood` 显式给兜底信号 | 进门判断分两层：**模型分是主力**（复用现成情绪批，多要一个 `scene` 字段，零新增调用），**关键词只在模型不可用时兜底**，而且兜底要"最近 15 分钟有亲密信号"而不是全放行。三条口径别改：① 模型分带 **30 分钟 TTL**，过期当"不知道"——模型默认 10 条一次，拿旧结论决定现在是最容易犯的错；② 拿不到分写 **-1（不知道）不是 0（明确闲聊）**，两者要分开处理；③ 结束场景只认**这一场开始之后**给出的判断（`ccb_scene_at > ccb_gate_at`），否则她刚进来就被旧结论赶出去。阈值 0.65/0.35 是滞回，别改成单阈值 |
| 闲聊也会进场景；300 岁的精灵被当成 0 岁拒绝；状态段永远慢一拍；「告别」被判成否定 | 四处都**不报错**：兜底词面有泛词（继续/里面/要我）、年龄正则没有数字边界（`300岁` 里匹配出 `00岁`）、状态段在写场景档**之前**算、单字「别」按整词匹配（告/区/特…构词） | `tests/test_v2286_scene.py::AuditRegressions`（六条，一条一个症状） | ① 兜底词面只留明确指向性亲密的说法——「要我」在记账词表里已因误报被删过一次，别再放回来；② 年龄正则必须双向卡边界 `(?<!\d)(\d{1,2})(?!\d)`；③ 状态段只能在推进**之后**算，否则进场那轮整段不出、之后永远慢一拍；④ 单字否定要看前一个字是不是构词前缀，且要用**绝对位置**取前一字（窗口从「别」开始时相对位置拿不到）；⑤ 坑中坑：`"" in 任何串` 恒为 True，句首「别」必须单独判空 |

## v3.0.0 之后新增的守则

这些是 v3.0.0 修掉的、删掉一行就会回来的毛病。动对应模块前先看用例：

| 症状 | 根因 | 守门测试 | 以后改这里要注意什么 |
|---|---|---|---|
| 熬夜越累，一过午夜精力反而回满；跨天那一段睡眠像没睡 | 精力跨天按**日历**给「新一天基线」，而不是从上次结算点连续结算 | `test_v300_services.py::EnergyMidnightTest`、`EnableCycleSwitchTest` | 跨天只许连续结算，睡眠恢复由日程里的睡眠段按分钟给出；别再引入「新一天起点」这类魔法数字。`enable_cycle=false` 时周期调制必须完全不生效。 |
| 状态落盘又把消息主循环冻住；序列化那几十毫秒里的新改动丢一轮 | 全量 `json.dumps` 同步跑在事件循环上，且落盘期间不追踪新改动 | `test_v300_state.py::EventLoopStallTest`、`AsyncSerializationFormatTest`、`AdaptiveFlushIntervalTest` | 序列化必须分块让出事件循环（2 MB 状态单次停顿 < 50 ms），用 dirty 世代号判断「这一轮有没有新改动」；体积上去自动拉长落盘间隔，上限 5 分钟。 |
| 手改坏的 `users` 值让某个用户永久失效，重载也修不好 | 迁移只修 role 级，`users[*]` 的 value 没逐项校验 | `test_v300_state.py::BadUserEntriesAreRepairedTest` | 加载时逐 value 校验，非 dict 重置成 `{}` 并落盘；`RoleScope` 对坏容器兜底重写。 |
| Windows 下状态落盘失败只剩一条 WARN，用户丢一段状态 | 失败不计数、不升级、不拉长退避 | `test_v300_state.py::WriteFailureHandlingTest` | 失败要计数（`flush_failures` / `consecutive_write_failures` / `last_write_error` 供诊断读）、连续失败升级告警、指数退避有上限，内存状态不许丢。 |
| 天气 key 又出现在日志或异常文本里 | 异常文本带完整 URL（含 `appid=`），被原样写进 warning | `test_v300_services.py::WeatherRedactionTest` | 任何异常文本进日志前先过 `weather.redact_secrets`（`appid=` / `api_key=` / `token=`），脱敏不依赖 debug。 |
| 系统时钟回拨后好感凭空跳涨、衰减冻结 | 未来时间戳被当成「刚刚发生」按满权重计入 | `test_v300_services.py::MoodClockRollbackTest` | 回拨时先把账本时间轴平移回 now；读 age 时对「未来」条目跳过，绝不夹成 0。 |
| 「恢复阶段倍率」又每次启动刷 WARN，用户 6 段自定义失效 | 旧 6 段配置没有迁移路径 | `test_v300_state.py::PhaseMultiplierMigrationTest` | 长度 = 6 判为旧格式，迁到 8 段默认并写回；别再拿「当前值 == 旧默认」当唯一判据覆盖用户的合法选择（见 `DefaultMigrationUserChoiceTest`）。 |

## 「现实→网友」/「变成手机发消息」是什么（v1.28.1 / v2.28.1 调查结论）

**不是插件的问题，也无需修。** 已按一位用户的反馈逐条核对过日志（其 QQ 号与昵称已隐去）。

### 数据

该用户 240 条 event_bus 记录：

| 类型 | 条数 | 昵称显示 |
|---|---|---|
| 真正的文字消息 | 39 | 正常显示昵称 |
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

695 个发件人里昵称含「关系/分组词」的只有 1 个，是对方自己起的昵称（其 QQ 号已隐去）。
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

1. 在插件根目录下跑 `python -m pytest tests -q`，必须 0 failed（允许既有 1 skipped）；
2. 新增行为至少有一条测试会在**旧代码**上变红；
3. 如果改的是语义反转（如认识天数），在 CHANGELOG 里写明「这是用户要求的反转」，
   并列清改了哪条既有断言、为什么；
4. 改了契约/门面字段：只加不改，同步 capabilities，补旧数据降级测试；
5. 改了睡眠/日程主干：先确认滚动分段、跨夜 carry、睡眠保护三件事没有被顺手改掉。
