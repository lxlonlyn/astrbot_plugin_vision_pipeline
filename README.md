# Vision Pipeline + AnimeTrace for AstrBot

一个把视觉任务封装成单一高层 LLM Tool `run_vision_pipeline` 的 AstrBot 插件。

目标是让 Main Persona 只负责“什么时候调用视觉能力”，不再手工编排 `Vision -> Search -> Vision`，并将 OCR、候选、rank、限流状态等中间数据保存在 Python 状态机里，避免 Main 改写或丢失关键信息。

## v0.6 核心变化

### 1. 修复 Multi-Region 坐标歧义

Primary Vision 现在必须输出：

```json
{"box":{"left":74,"top":56,"right":521,"bottom":915}}
```

不再使用四元素数组。插件仍兼容 v0.5 风格旧数组，但会结合“左侧/右侧/底部”等 label 进行坐标顺序恢复。

### 2. 多实体识别加入“实体层级消歧”

强 OCR / Studio / 版权信息命中后，由廉价 Search Provider 先规划最多 2 条高信息量查询，再把网页结果整理成每个 region 的候选池。候选显式区分：

```text
visual identity
entity type
work/project
related entities
```

因此“虚拟艺人”“音乐同位体/衍生声库”“游戏/动画角色”“剧情对应角色”“声源”不再因为彼此有关联就被写成 `A / B` 同一个身份。

### 3. Final Vision 只做视觉裁决

如果某 region 已有 grounded candidate pool，Final Vision 只能从该 pool 选择具体身份或 abstain；相关实体只能写入 `related_entities`。最终 Main 使用结构化字段渲染答案，而不是复述模型可能混合的 `overall_answer`。

### 4. 成本控制不变

多人图仍是“一条 Pipeline”：一次 Primary Vision + 少量全局搜索 + 一个廉价候选整理 + 一次 Contact Sheet Final Vision。不会为每个人启动独立完整 Pipeline。AnimeTrace 多人物模式仍默认关闭。

## v0.5 核心变化

### 0. 当前图片优先：不再复用历史旧图

`run_vision_pipeline` 只读取**当前消息直接携带的图片**，或当前 `Reply` 组件里仍能直接取得的图片。若当前 Tool Call 没有图片，直接返回“请把图片和问题一起发送”，并明确要求 Main 不得从历史上下文、temp 目录或文件列表猜图。

这与“消息文本持久化、图片不持久化”的部署方式一致，也避免群聊里拿错上一张图。

### 1. Strong Text Anchor / OCR-led Hybrid Grounding

版权、Studio、企划名、Logo、作品名等文字可作为强独立证据。若 Primary Vision 读到类似 `©2025 THINKR INC. / KAMITSUBAKI STUDIO` 的强锚点，即使 AnimeTrace 有候选，也允许最多 1~2 次文本驱动搜索，将网页候选**追加**到 AnimeTrace 不可变候选后，再交给 Final Vision 统一比对。

低置信 AnimeTrace (`not_confident=true`) 不再享受代码级 rank1 强制保底；它只是候选，不能压过强 OCR/Logo/版权证据。

### 2. Multi-Region：多人 / 多物品 / 货架 / 展柜

不为每个对象启动一套完整 Pipeline，也不依赖 AnimeTrace 的人物框作为唯一分割来源。

流程：

```text
原图 -> Primary Vision 一次定位主要 regions
     -> 本地 PIL 裁剪 + Contact Sheet（整图概览 + A/B/C...局部）
     -> 可选整图级 OCR/版权搜索（最多少量查询，不按 crop 重复）
     -> Final Vision 一次识别所有区域
```

默认 `animetrace_multi_object_enabled=false`，因为 AnimeTrace 有人物数量限制、容量失败风险，且不适合一般货架/商品对象。即使开启，它也只是辅助候选，不参与唯一分割。

区域裁剪默认顶部扩边较大，用于保留 Halo、帽子、兽耳；同时保留左右/下方服装和商品标签。

### 3. Reviewer / AnimeTrace 保护收紧

只有 `not_confident=false` 的 AnimeTrace rank1、且没有强文本锚点冲突时，才允许 `OVERCAUTIOUS -> medium fallback`。低置信 AnimeTrace、多人任务、强 OCR 冲突不会被 Reviewer 强行救回。Reviewer 继续 fail-soft，超时不拖垮整个 Tool。

## v0.4 核心变化


### 0. v0.4 重点修复：纠错候选溯源 + AnimeTrace rank1 保护

v0.4 针对实际测试中出现的两个问题做了代码级修复：

1. Main 曾把“模型自己否定过的候选”误塞进 `rejected_candidates`。现在插件只接受**用户侧有来源的否定**：当前用户消息明确点名该候选，或当前消息是在纠正上一轮 `LOCKED` 身份。模型/搜索阶段讨论过的名字不会自动变成黑名单。
2. v0.4 曾对 AnimeTrace rank1 做强保底；v0.5 已收紧：只有 `not_confident=false`、无强文本锚点冲突时才允许 medium fallback，低置信 AnimeTrace 不会被代码强行救回。

此外：

- Reviewer 默认 12 秒内部超时，失败/超时直接跳过，**不会再让整个 Tool 因最后 Reviewer 卡住而触发 AstrBot 120 秒总超时**。
- Search Planner / Synth / AnimeTrace 文本核验 Worker 也增加超时预算，fallback 更容易在总超时前安全结束。
- AnimeTrace 同图缓存永久保存原始未过滤候选；每次请求的用户否定仅作用于当前视图，不污染缓存。
- 身份未确认时返回 `LOCKED_FACTS.IDENTITY_STATUS=UNCONFIRMED`，并只暴露直接视觉观察，Main 不应根据 EVIDENCE 再猜身份。
- 身份确认时返回 `IDENTITY_STATUS=CONFIRMED`。

### 1. AnimeTrace Fast Path

对于动漫、游戏、Galgame、VTuber/VUP 等二次元角色身份识别：

```text
Main
  -> run_vision_pipeline
      -> Vision Primary
      -> AnimeTrace
          -> 有候选：保留原始 rank，不经过通用 Search Synth
               -> not_confident=false：直接 Vision Verify
               -> not_confident=true：最多 1 次 rank1 定向网页核验 -> Vision Verify
          -> 无候选/限流/不可用：Tavily discovery -> Search Synth -> Vision Verify
      -> Final Reviewer（可选）
      -> FINAL
```

AnimeTrace 一旦返回候选，其 `canonical_name / rank / not_confident` 被视为不可重排的专业视觉候选证据。廉价 Search 模型不能删除、替换或重新排列这些候选。

`not_confident=true` 只表示需要进一步视觉核验，不代表 rank1 错误。

### 2. 网页“未提到外观细节”不再算反证

针对 AnimeTrace rank1 的定向网页核验只负责：

- 核实名字是否是真实可查实体；
- 核实作品/企划归属；
- 尽量补充可靠中文名；
- 记录真正的 HARD_CONFLICT。

“网页没写某个发夹/发色”只属于 `missing_evidence`，不能因此否定 AnimeTrace 候选。

### 3. 中文名对照

AnimeTrace 经常返回日文原名。默认开启：

```text
animetrace_resolve_chinese_names = true
```

插件会在 rank1 的定向网页核验中尝试寻找可靠中文名。内部同时保留：

```text
canonical_name  = AnimeTrace 原始/官方名字，不可被覆盖
 display_name   = 有可靠来源时使用中文名，否则等于 canonical_name
 aliases_zh     = 网页明确出现的中文别名
```

最终输出优先展示：

```text
中文名（原名）
```

如果没有可靠中文名，继续使用 AnimeTrace 原名，不自行音译猜测。

候选中文名/作品核验结果默认缓存 24 小时，减少重复 Tavily 请求。

### 4. 固定子任务，避免任务漂移

Pipeline 在代码中固定 `requested_subtasks`，例如：

```text
identity
ocr
meme
```

如果用户同时要求“识别角色 + 读文字 + 解释表情包”，Final Vision 必须分别回答三个子任务，不能因为表情包含义很确定就丢掉角色身份任务。

结果包含分项置信度：

```text
SUBTASK_CONFIDENCE: {"identity":"medium","ocr":"high","meme":"high"}
```

整体置信度取所请求子任务中最低值，避免“梗解释 high”掩盖“身份 low”。

### 5. Reviewer 检测过度谨慎

Final Reviewer 现在除了检查错误确认，还会检测：

```text
AnimeTrace 有 rank1
+ 未被用户否定
+ 没有 HARD_CONFLICT
+ Final Vision 仅因 not_confident / 网页缺少详细外观文字而弃权
```

这种情况返回 `OVERCAUTIOUS`。

不会触发第三次 Vision。v0.5 中该兜底只允许用于 `not_confident=false` 的高置信 AnimeTrace rank1；低置信候选、强 OCR/版权锚点、多对象任务不会被强制保留。

### 6. LOCKED_FACTS

Tool Result 会额外返回：

```text
LOCKED_FACTS:
IDENTITY_STATUS: CONFIRMED | UNCONFIRMED
IDENTITY_EXACT: ...
IDENTITY_CANONICAL: ...
WORK_EXACT: ...
OCR_EXACT: ...
```

Main 应逐字复制这些字段，不要重新 OCR、纠错或同义改写。

推荐在 Main Persona 中加入通用规则：

```text
任何专业 Pipeline 返回的 LOCKED_FACTS 都属于锁定事实。
引用其中字符串时必须逐字复制，不得纠错、同义改写或重新识别。
```

## Rate limit / 服务繁忙保护

AnimeTrace 部分保留并加强：

1. 每个 Pipeline 对同一张图最多实际调用 AnimeTrace 一次。
2. 全局 `asyncio.Lock` 串行 AnimeTrace 请求。
3. `animetrace_min_interval_seconds` 控制相邻请求最小间隔。
4. 同图 SHA256 缓存，默认 30 分钟。
5. HTTP 429、17702、17731 自动进入冷却并 fallback 到 Tavily。
6. 17728（使用上限）进入更长 quota cooldown。
7. 不在同一任务中立即重试 AnimeTrace。
8. AnimeTrace 模型列表动态获取并缓存，不硬编码模型 ID。
9. 中文名/候选资料验证另有 24 小时缓存。

## 安装 / 更新

将插件 ZIP 在 AstrBot WebUI：

```text
扩展功能 -> 插件 -> 安装插件
```

上传即可。

如果从旧版本更新，建议删除/替换旧插件后重载插件或重启 AstrBot。

无需修改 uv/site-packages 中的 AstrBot 源码。

## 推荐配置

```text
Vision Provider: Gemini 3.8 Flash
Search Provider: DeepSeek Flash
Reviewer: ON
Reviewer timeout: 12s
Search worker timeout: 30s
force_grounding_entity_identity: ON
AnimeTrace: ON
AnimeTrace trigger: auto
AnimeTrace min interval: 2s
AnimeTrace cooldown: 90s
AnimeTrace quota cooldown: 600s
AnimeTrace cache TTL: 1800s
AnimeTrace not_confident 定向网页核验: ON
AnimeTrace rank1 medium fallback: ON（仅 not_confident=false 时可能生效）
AnimeTrace 中文名解析: ON
中文名/资料缓存: 86400s
Strong text anchor search: ON
Strong text anchor max queries: 2
Multi-Region: ON
Multi-Region contact sheet: ON
Multi-Region max regions: 6
Multi-Region Search Planner: ON
Multi-Region Candidate Synth: ON
Multi-Region Strict Typed Answer: ON
AnimeTrace multi-object: OFF
Tavily generic fallback max queries: 3
Tavily extract: OFF
```

AstrBot 原生网页搜索保持：

```text
web_search = ON
websearch_provider = tavily
websearch_tavily_key = [key1, key2, ...]
```

插件直接复用 AstrBot 原生 Tavily，也能使用 AstrBot 的多 Key 轮询/故障切换。

## Main Persona / Tool

Main Persona 只需要勾选：

```text
run_vision_pipeline
```

旧 `vision` / `search` SubAgent 如果只为识图服务，可以关闭或删除。

Main 视觉规则可保持非常短：

```text
所有真正依赖图片内容的任务调用 run_vision_pipeline。
调用前不要自行描述图片、猜角色或作品。
Pipeline 返回后采用 FINAL_ANSWER；LOCKED_FACTS 必须逐字复制。
若 IDENTITY_STATUS=UNCONFIRMED，Main 严禁从 EVIDENCE 或历史候选中自行猜身份。
只有用户本人明确否定的名字才允许传入 rejected_candidates；模型/工具自己排除过的候选不得加入。
```

## 图片输入策略（v0.5）

Pipeline **不再保存或复用历史图片**。它只读取当前消息直接携带的图片，或当前 Reply 组件中仍能直接取得的图片。

因此如果群聊中先发了一张图，过一会儿再单独问“这张图是谁”，而当前消息没有图片/可读取的引用图片，Pipeline 会直接返回：请重新附图。它不会等待、不会去 temp 目录找最近 jpg，也不会拿历史旧图猜。

这与“文本消息持久化、图片不持久化”的部署策略保持一致。

## 调试

查看状态：

```text
/vpipe_status
```

绕过 Main Persona 直接测试：

```text
/vpipe 这张图里的角色是谁？
```

调试期建议：

```text
return_debug_meta = true
debug_log = true
```

典型 AnimeTrace Fast Path：

```json
{
  "vision_calls": 2,
  "search_calls": 1,
  "reviewer_calls": 1,
  "animetrace_calls": 1,
  "grounding_path": "animetrace_plus_targeted_web"
}
```

其中 `search_calls=1` 通常只是 rank1 的姓名/作品/中文名定向核验，不再进行 3 次通用搜索 + Search Synth。

## 成本特点

- OCR/UI/普通图像理解：通常 1 次 Vision。
- AnimeTrace 命中且关闭中文名解析、且无需网页核验：2 次 Vision + 1 次 AnimeTrace。
- AnimeTrace 命中且需要中文名 / not_confident：2 次 Vision + 1 次 AnimeTrace + 最多 1 次 Tavily + 1 次廉价文本核验。
- AnimeTrace 无候选/限流：才进入最多 3 次 Tavily 的通用 fallback。
- 不再让 Search Synth 重排 AnimeTrace 候选。
- 不再出现每次搜索都重新携带图片调用 Gemini 的多轮 Tool Loop。
- Reviewer/Search Worker 均有内部超时预算，尽量避免完成大部分工作后因最后一步卡住导致整条 Pipeline 重跑。

## 隐私说明

启用 AnimeTrace 后，在适合专用动漫/游戏角色识别的任务中，当前图片会发送给第三方 AnimeTrace 服务用于识别。若部署环境不希望上传图片，可关闭 `animetrace_enabled`，Pipeline 会退化到 Gemini Vision + Tavily grounding。

## v0.7 progressive retrieval

Multi-object identity resolution now avoids rebuilding or injecting a full background dossier on every request. The workflow stores only a lightweight source index (source name, entity types, names and works) for 7 days in AstrBot plugin data (with memory fallback). On a cache miss it uses at most two web searches: one to discover the source ecosystem and one to discover candidates from region-specific visual traits. On a cache hit it normally uses only the region-specific search.

The expensive final Vision call receives compact candidate cards rather than raw search snippets. Each card contains at most a name, entity type, work, three externally supported visual traits, one source URL and short relations. Primary Vision guesses are deliberately excluded from the Multi-Region search plan to prevent candidate lock-in.

`pipeline.yaml` documents the intended state machine and can be reviewed independently from the implementation.

