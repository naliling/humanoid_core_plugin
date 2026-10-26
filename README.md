<style>
.hc-table{width:100%;border-collapse:collapse;font-size:14px}
.hc-table th{text-align:left;padding:10px 12px;border-bottom:2px solid #dbeafe;color:#1d4ed8;background:#eff6ff;font-weight:700}
.hc-table td{padding:9px 12px;border-bottom:1px solid #eef2ff;vertical-align:top}
.hc-table code{background:#eff6ff;color:#1d4ed8;border-radius:6px;padding:1px 6px;font-size:13px}
.hc-grid{width:100%;border-collapse:separate;border-spacing:8px;font-size:14px}
.hc-grid td{width:50%;background:#eff6ff;border-left:3px solid #2563eb;border-radius:8px;padding:10px 12px;vertical-align:top}
.hc-grid b{color:#1d4ed8}
.hc-note{background:#eff6ff;border-left:3px solid #60a5fa;border-radius:8px;padding:10px 14px;font-size:14px;color:#1e3a8a;line-height:1.9}
.hc-chip{display:inline-block;background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.38);border-radius:999px;padding:4px 13px;margin-right:8px;font-size:12px;letter-spacing:1px}
</style>

<div class="hc-hero" style="background:linear-gradient(135deg,#1d4ed8 0%,#2563eb 55%,#60a5fa 100%);border-radius:20px;padding:30px 28px;color:#fff;box-shadow:0 12px 32px rgba(37,99,235,.28)">
  <div style="font-size:12px;letter-spacing:4px;opacity:.8">ASTRBOT · 人形化伴侣</div>
  <div style="font-size:36px;font-weight:800;margin:8px 0 10px;line-height:1.2">人形化伴侣</div>
  <div style="font-size:15px;line-height:1.9;opacity:.95">她有身体，有日程，有情绪。<br>不是一句人设，是一整天的生活。</div>
  <div style="margin-top:16px;font-size:12px;opacity:.9"><span class="hc-chip">身体</span><span class="hc-chip">日程</span><span class="hc-chip">情绪</span></div>
</div>

![身体](https://img.shields.io/badge/-%E8%BA%AB%E4%BD%93-2563eb?style=flat-square) ![日程](https://img.shields.io/badge/-%E6%97%A5%E7%A8%8B-2563eb?style=flat-square) ![情绪](https://img.shields.io/badge/-%E6%83%85%E7%BB%AA-2563eb?style=flat-square) ![好感度](https://img.shields.io/badge/-%E5%A5%BD%E6%84%9F%E5%BA%A6-2563eb?style=flat-square) ![亲密状态](https://img.shields.io/badge/-%E4%BA%B2%E5%AF%86%E7%8A%B6%E6%80%81-2563eb?style=flat-square) ![生理模拟](https://img.shields.io/badge/-%E7%94%9F%E7%90%86%E6%A8%A1%E6%8B%9F-2563eb?style=flat-square) ![生理周期](https://img.shields.io/badge/-%E7%94%9F%E7%90%86%E5%91%A8%E6%9C%9F-2563eb?style=flat-square) ![睡眠](https://img.shields.io/badge/-%E7%9D%A1%E7%9C%A0-2563eb?style=flat-square) ![天气](https://img.shields.io/badge/-%E5%A4%A9%E6%B0%94-2563eb?style=flat-square) ![时间](https://img.shields.io/badge/-%E6%97%B6%E9%97%B4-2563eb?style=flat-square) ![位置地区](https://img.shields.io/badge/-%E4%BD%8D%E7%BD%AE%E5%9C%B0%E5%8C%BA-2563eb?style=flat-square) ![输入状态](https://img.shields.io/badge/-%E8%BE%93%E5%85%A5%E7%8A%B6%E6%80%81-2563eb?style=flat-square) ![多角色](https://img.shields.io/badge/-%E5%A4%9A%E8%A7%92%E8%89%B2-2563eb?style=flat-square) ![长期记忆](https://img.shields.io/badge/-%E9%95%BF%E6%9C%9F%E8%AE%B0%E5%BF%86-2563eb?style=flat-square)

> 插件负责她的身体、日程、情绪和关系背景；具体怎么说话，仍然由大模型按人设自己决定。

## 指令

查状态的这几条谁都能用，**直接打，不带前缀**：

<table class="hc-table">
<tr><th style="width:40%">指令</th><th>作用</th></tr>
<tr><td><code>/你的状态</code></td><td>精力、身体轴、生理、天气、日程、关系、注意力</td></tr>
<tr><td><code>/亲密状态</code></td><td>亲近欲离门槛多远、在不在场景里（仅私聊）</td></tr>
<tr><td><code>/查看日程</code></td><td>她今天过出来的日程，动态现排、不预排未来</td></tr>
<tr><td><code>/时间 城市</code></td><td>指定城市的当前时间、星期与节日</td></tr>
<tr><td><code>/好感度</code></td><td>情绪档案总览；管理员加「排行榜」看排名</td></tr>
<tr><td><code>/情绪详情</code></td><td>详细情绪档案，含基线值与交互轮次</td></tr>
<tr><td><code>/情绪日志</code></td><td>最近的情绪波动记录</td></tr>
<tr><td><code>/叫我 昵称</code></td><td>设置她对你的称呼，没设过时她会自动认一次</td></tr>
<tr><td><code>/拟人帮助</code></td><td>显示指令帮助</td></tr>
</table>

**管理员 · 改配置**

<div class="hc-note">下面这些挂在「拟人」组里，要带 <b>/拟人</b> 前缀，且只有管理员能用。</div>

<table class="hc-table">
<tr><th style="width:40%">指令</th><th>作用</th></tr>
<tr><td><code>/拟人 时间 城市</code></td><td>设置她所在的城市（时区），只改当前这个机器人</td></tr>
<tr><td><code>/拟人 设置</code></td><td>看常用项；写成「/拟人 设置 间隔 30」改一项，立即生效</td></tr>
<tr><td><code>/拟人 参照名称 名字</code></td><td>设定注入里代表她的名字（默认跟人设）</td></tr>
<tr><td><code>/拟人 诊断</code></td><td>排查模型选择、时区是否生效、上下文与状态文件大小</td></tr>
<tr><td><code>/拟人 重载</code></td><td>面板改完进阶项，用它刷新配置</td></tr>
</table>

**管理员 · 动数据**

<table class="hc-table">
<tr><th style="width:40%">指令</th><th>作用</th></tr>
<tr><td><code>/拟人 重置日程</code></td><td>立即重新决定她当前这一段</td></tr>
<tr><td><code>/拟人 日程打断</code></td><td>让她放下手上的事，换一件（不是继续）</td></tr>
<tr><td><code>/拟人 日程继续</code></td><td>把当前这一段往后延长，不另外生成</td></tr>
<tr><td><code>/拟人 重置状态</code></td><td>重置精力、社交能量与生理周期</td></tr>
<tr><td><code>/拟人 重置情绪</code></td><td>把<b>你自己</b>的情绪重置到初始值</td></tr>
<tr><td><code>/拟人 设置好感度 数值</code></td><td>手动设置自己的好感度（0-100）</td></tr>
<tr><td><code>/拟人 批量好感度 QQ:值,QQ:值</code></td><td>批量导入好感度</td></tr>
<tr><td><code>/拟人 查看昵称</code></td><td>看所有用户设置的昵称</td></tr>
</table>

## 功能

<table class="hc-grid">
<tr>
  <td><b>身体状态</b>　精力困意饥饿</td>
  <td><b>睡眠</b>　睡不够会起不来</td>
</tr>
<tr>
  <td><b>生理周期</b>　8 个阶段各有体感</td>
  <td><b>动态日程</b>　一天会被打断</td>
</tr>
<tr>
  <td><b>日程打断</b>　叫住她就换事</td>
  <td><b>挂起续做</b>　忙完接着做</td>
</tr>
<tr>
  <td><b>梦</b>　睡着会做梦</td>
  <td><b>情绪</b>　会随互动变化</td>
</tr>
<tr>
  <td><b>好感度</b>　关系会变深</td>
  <td><b>认识天数</b>　记得认识多久</td>
</tr>
<tr>
  <td><b>时间时区</b>　按她的城市过</td>
  <td><b>天气</b>　她那边什么天</td>
</tr>
<tr>
  <td><b>节日季节</b>　知道今天什么日子</td>
  <td><b>正在输入</b>　回复时显示输入中</td>
</tr>
<tr>
  <td><b>多角色</b>　每个角色各自生活</td>
  <td><b>长期记忆</b>　按你的话读记忆</td>
</tr>
<tr>
  <td><b>未完事项</b>　记着还开着的事</td>
  <td><b>对话间隔</b>　知道你多久没来</td>
</tr>
</table>

## 联动

装了「自主拟人社交」时，她把身体、日程、关系、称呼与亲密状态一起导出去，由对方决定什么时候主动开口；没装也能独立跑，只是没人替她先说话。

| 对比 | 人形化伴侣 | 自主拟人社交 |
|---|---|---|
| 主要职责 | 维护身体、情绪、日程、关系 | 决定是否主动联系、联系谁、聊什么 |
| 是否主动发消息 | 不负责 | 负责 |
| 日程与状态 | 生成并维护 | 读取作为决策参考 |

## 安装

1. AstrBot「本地安装 zip」**不覆盖同名目录**：装过旧版先卸载（**不要勾**「删除配置」「删除数据」），再上传新 zip。
2. **装完第一件事：改「她所在的城市」**（`/拟人 时间 大阪`，或面板里的城市项）。它决定她的钟点、作息、天气和季节；不改会静默退回服务器时区。
3. 确认 AstrBot 里的角色人设已经写好——日程语气与生活特质都从这份人设里读。
4. 需要天气就填 `weather_api_key`（OpenWeather 免费档够用），其余先用默认值跑。

配置在 `data/config/astrbot_plugin_humanoid_core_config.json`，身体状态在 `data/plugin_data/humanoid_core/state.json`，卸载这两个都不动。依赖只有 `aiohttp`。

## 城市与时区

她按「她所在的城市」过日子：`/时间 城市` 可以查别的城市，但**她的身体和日程只按这个**。填表里有的名字（河源/北京/大阪/莫斯科），或者直接填 IANA 时区名（`Europe/Moscow`）。

表里没有的名字会**静默退回服务器时区**——`/拟人 诊断` 的【时区】会直接告诉你有没有生效。地名表覆盖九千多条，中国、日本、俄罗斯的地级与县级驻地大多都认，写「广东省」「大阪市」这类带后缀的也认。多 bot 可以在各自的 `/拟人 时间 城市` 里单独设，互不影响；`/时间 城市` 谁都能用，只是查时间。

天气城市填英文名+国家码（`Beijing,CN`）；留空则跟着城市配置走（且必须是英文名）。两处不一致会出现「说在北京却报河源的天气」。

## 数据与边界

- 她**不做长期记忆**：不建记忆库、不存聊天记录。
- 她只留两样**短**东西：最近 12 小时内你说过的一句原话，以及手上还开着的未完事项（最多三条、最多放三天，事情了结就消失）。
- 这两样都随每条回复递给她一句事实，**不落会话历史**：上下文被压缩掉之后它们还在。
- 装了「我会牢牢记住你」时两边会接上：每轮按你刚说的话去读这个人的记忆，她自己生活里的事真正结束时补一条回去；没装照常跑。

## 关键配置项

面板上共 132 项（多数带【进阶】标记），这里列真正需要动的：

| 项 | 默认 | 什么时候调 |
|---|---|---|
| `timezone_city` | 河源（记得改~） | **必改**。决定她的钟点、作息、天气、季节 |
| `她所在的角色人设` | AstrBot 那边设 | 日程按它排；写她的语气、身份、说话习惯 |
| `use_llm_schedule` | 开 | 关掉则日程改用本地模板（更省额度，情节也更死板） |
| `llm_daily_call_budget` | 200 | 每日模型调用**次数**上限（不是 token），多 bot 共享一份 |
| `llm_idle_silence_minutes` | 60 | 连续这么久没人说话就停掉后台调用，有人一说话立刻恢复 |
| `mood_use_llm_for_delta` | 开 | 关掉则情绪**完全本地**判断，零额外调用 |
| `mood_llm_interval_messages` | 10 | 每人每几条消息用模型分析一次情绪（**按人算**） |
| `mood_data_retention_days` | 7 | 超过这么多天没说话，情绪档案清回初始值（称呼保留） |
| `mood_negative_streak_required` | 2 | 连着几句难听话才扣好感；1=每句都扣 |
| `mood_negative_softening` | 开 | 底部减速 + 情绪韧性（好感越低扣得越少、关系越深越抗打击） |
| `mood_recovery_bonus` | 开 | 道歉快速回温 + 低于基线时暖话加成 + 连着哄加速消气 |
| `cooling_enabled` | 开 | 冷静期：好感跌破门槛后她冷着一段时间，且这段时间不主动找他 |
| `cooling_affection` | 10 | 好感低于它就进入冷静期 |
| `cooling_minutes` | 15 | 冷静期语气持续多久（分钟） |
| `cooling_no_proactive_days` | 3 | 冷静期里不主动找 TA 的天数 |
| `auto_identify_user` | 关 | 开则注入里用真名区分「她」和「对方」 |
| `auto_nickname` | 开 | 从消息自带的名字认一次称呼；含数字/emoji/占位词一律不认 |
| `schedule_interrupt_on_call` | 开 | 私聊里明确叫住她（「在吗/你在干嘛/陪我」）时，不熬安静间隔也算一次来访 |
| `schedule_interrupt_quiet_minutes` | 45 | 距上一条私聊至少隔这么久才算「有人来访」；0 = 每条私聊都算（多人部署慎用） |
| `schedule_continue_minutes` | 45 | `/拟人 日程继续` 一次把当前这一段往后延长多久 |
| `schedule_pause_ttl_minutes` | 180 | 被打断的事保留多久（期间她可以自己回去接着做） |
| `weather_api_key` | 空 | OpenWeather 密钥（免费档就够） |
| `soma_enabled` | 开 | 关掉则只剩精力一个数 |
| `input_state_enabled` | 开 | 私聊回复时显示「正在输入」（仅 NapCat） |

改完面板里的进阶项，用 `/拟人 重载` 刷新；`/拟人 设置` 改的项立即生效。

## 更新日志

见 [更新日志](CHANGELOG.md)。仓库：https://github.com/naliling/humanoid_core_plugin

<details>
<summary>深入一点：她的一天是怎么跑起来的</summary>

- **状态进用户消息，背景进系统提示**：每轮在变的东西（她对 TA、心情、身体感觉）进用户消息；几点、天气、季节、她今天在做什么进背景——塞进用户消息里，模型会拿它们当开场白。
- **给体感，不给数值**：「有点困」是递给她的；「好感 46/100」这种体检单留在 `/你的状态` 里给你看。
- **同一件事只说一遍**：关系位置、心气、精力、想不想说话，各自有且只有一个来源。
- **日程按 15 分钟一个决策窗**：到点或掷中「改主意概率」才排下一段，不预排未来；关掉模型就用按日期轮换的本地模板，任何时刻都有段可做。
- **有人打破一段安静来找她**：给她一次中途换事的机会，旧活挂起，忙完可以自己回去接着做。
- **睡眠不受消息打扰**：睡着那一段不会因为来消息就重排，但会记下「被吵到」；刚醒的一小段时间她还没完全缓过来。
- **身体按时间积分**：睡眠债、久坐、饥饿随时间推进，停机后按离线时长补算（上限 72 小时）。
- **情绪本地先判**：模型结果只占 0.3 权重融合；本地已经判负明确时，那一次模型调用直接跳过。
- **状态写盘节流**：变更后最多每 60 秒整份重写一次，原子替换，`/拟人 诊断` 会显示文件大小与用户数。

</details>
