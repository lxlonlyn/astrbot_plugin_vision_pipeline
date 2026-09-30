# Changelog

## v0.8.0

- Multi-Region 关键路径移除 Search Planner + Candidate Synth 两次文本 LLM；Primary Vision 直接输出不含候选名的 `search_terms`，插件把网页预算直接花在各 region 上。
- 修复 v0.7 实测的 Candidate Synth 超时：搜索整理不再依赖该 Worker，因此其超时不再导致空候选池。
- 默认两人物图按 region 各做一次开放检索；不再每轮先重建完整企划背景。
- 新增 Tavily `include_images` 视觉参考：复用 AstrBot 全局 Tavily 多 Key，并对 401/403/429/432 做 failover；失败自动回退 AstrBot 原生 Tavily。
- 搜索结果直接生成短 Search Card（source_ref/title/url/snippet），不再经过自由长篇 Candidate Synth。
- 新增网页视觉参考：优先使用 Tavily 搜索结果绑定图片，否则提取页面 og:image / twitter:image / 主图；TARGET 与 WEB REF 拼成单张 Reference Sheet。
- Final Multi-Region Vision 改为 `MATCH | UNRESOLVED`；MATCH 必须绑定同 region 的 source_ref。
- 代码级 grounding gate 要求具体 identity 能在选中 Search Card 的网页标题中找到；snippet 中只出现的声源/原型/相关人物不能冒充 visual identity。
- high confidence 需要实际网页参考图 + 至少两项视觉对应；纯文字关系证据最高 medium。
- UNRESOLVED 执行严格证据清洗：删除模型猜出的身份、作品、related_entities 与实体主张，只保留 Primary 直接视觉观察。
- LOCKED_FACTS 增加 `MULTI_CONFIRMED / MULTI_PARTIAL / MULTI_UNCONFIRMED`、逐 region STATUS，以及 `DO_NOT_INFER_IDENTITY=TRUE`，防止 Main 从 Evidence 二次猜身份。

## v0.7.0

- Multi-Region search switched from `guess -> verify guesses` to progressive `source ecosystem -> region visual candidates -> verify`.
- Primary Vision `possible_leads` are no longer passed to the Multi-Region search planner, reducing candidate lock-in.
- Added lightweight persisted source-index cache (default 7 days, stored under AstrBot plugin_data with memory fallback); cache hits normally need only one region-specific web search.
- Search snippets for Multi-Region are compacted to at most 3 results x ~420 chars before the cheap synth model.
- Candidate Synth now emits compact candidate cards with `visual_traits` and one source URL, max 3 candidates per region.
- Final Vision no longer receives raw web snippets; only the compact source index and candidate cards.
- Added `MATCH / NONE_OF_ABOVE / UNRESOLVED` decisions so the verifier is not forced to choose a bad candidate.
- Added code-level confidence ceiling: relationship-only grounding or missing candidate visual traits cannot become `high`.
- Added `pipeline.yaml` and repo-ready `.gitignore` / `REPO_SETUP.md` for easier future Git-based maintenance.

## v0.6.0

- Multi-Region 区域坐标改为具名 `left/top/right/bottom`，彻底消除 `[x1,y1,x2,y2]` / `[top,left,bottom,right]` 顺序歧义；仍兼容旧数组并按“左/右/顶部/底部”位置提示做启发式恢复。
- Contact Sheet 改为统一读取具名区域坐标，修复双人物图可能裁成“上半图/下半图”而不是“左人物/右人物”的问题。
- 新增廉价 `Multi-Region Search Planner`：强版权/Studio OCR 命中后，不再把 2 次 Tavily 都浪费在重复版权字符串；第二个查询优先解决“虚拟艺人 vs 音乐同位体/衍生声库 vs 作品角色/对应角色”等实体层级歧义。
- 新增 `Multi-Region Candidate Grounder`：将网页结果整理为按 region 的候选池；候选显式包含 `entity_type`、`work`、`related_entities`，禁止把有关联的两个实体当同义身份。
- Final Multi-Region Vision 在存在候选池时只能从该 region 的 grounded candidates 中选择或弃权，降低“凭模型记忆把相关人物认成同一个人”的风险。
- Multi-Region 输出改为 `visual_identity / canonical_name / display_name / entity_type / related_entities` 分层；Main 的 `LOCKED_FACTS` 增加 `ENTITY_A_EXACT / ENTITY_A_TYPE / ENTITY_A_WORK` 等字段。
- 默认启用 `multi_region_strict_typed_answer`：最终展示只使用 visual identity，不再把“艺人 / 音乐同位体 / 剧情角色”用斜杠拼成一个身份。
- 保持 current-image-only：没有当前图片时仍直接要求用户重新附图，不等待未来图片、不复用历史图、不扫描 temp 猜图。

## v0.5.0

- 改为 current-image-only：不再复用历史图片缓存，也不从 temp 目录猜旧图；当前消息/当前 Reply 没有可直接读取的图片时返回 `IMAGE_STATUS=REQUIRED` 并要求用户重新附图。
- 新增强文字锚点 `strong_text_anchors`：版权、Studio、企划名、Logo、作品名等 OCR 可触发少量文本驱动 Web Grounding。
- AnimeTrace 有候选时，若存在强文字锚点，可走 Hybrid Grounding：AnimeTrace 原候选不可变保留，Web 只追加额外候选，不覆盖/重排。
- `not_confident=true` 的 AnimeTrace rank1 不再享受代码级 medium 强制保底；低置信候选可被原图、强文字锚点和网页证据否决。
- 新增 Multi-Region 模式：Primary Vision 一次输出 0..1000 归一化 regions；插件本地用 Pillow 裁剪并生成单张 Contact Sheet；Final Vision 一次识别所有区域，避免每个对象重复完整 Pipeline/系统提示。
- Multi-Region 默认不调用 AnimeTrace (`animetrace_multi_object_enabled=false`)；AnimeTrace 即使启用也只作辅助候选，不作为唯一人物检测/分割器。
- Multi-Region 只做整图级少量搜索，不按 crop 重复检索；有强 OCR/版权锚点时优先使用文字信息缩小作品/角色池。
- 新增区域裁剪扩边配置，默认顶部扩 30% 以保留 Halo/头饰，左右与底部保留服装/商品标签。
- Reviewer 规则收紧：只有高置信 AnimeTrace (`not_confident=false`) 且无强文本冲突时才允许 `OVERCAUTIOUS` fallback；继续 fail-soft。


## v0.4.0

- `rejected_candidates` 增加用户侧 provenance guard：只有当前用户明确点名否定，或明确纠正上一轮 LOCKED identity 时才会真正进入拒绝集合；Main/模型内部讨论过的候选不会被误加入黑名单。
- AnimeTrace 图片缓存改为永久保存“原始未过滤候选”；用户拒绝只作为单次请求视图，不会污染同图缓存。
- AnimeTrace rank1 增加代码级保护：未被用户明确拒绝、且定向网页核验没有 HARD_CONFLICT 时，Final Vision 即使凭模型记忆声称冲突，也不能把 rank1 彻底丢弃；至少保留为 medium confidence。
- Final Vision / Reviewer 的 HARD_CONFLICT 规则增加来源约束：用户明确否定或网页可靠资料才可作为权威硬冲突，模型自身“我记得某角色不是这样”只算软疑虑。
- Reviewer 改为 fail-soft：默认 12 秒内部超时，超时/异常直接跳过，已完成的 Vision Verify 结果继续返回，不再让整个 `run_vision_pipeline` 因 Reviewer 卡住触发 AstrBot 120 秒 Tool timeout。
- Search Planner / Search Synth / AnimeTrace 网页资料整理增加廉价 Worker 超时预算，降低 fallback 路径把整个 Tool 拖到 120 秒的概率。
- 未确认身份时新增 `LOCKED_FACTS.IDENTITY_STATUS=UNCONFIRMED`，并只向 Main 暴露 Primary 的直接视觉观察，不再把被淘汰候选的强烈实体主张作为 EVIDENCE，减少 Main 二次越权推断。
- `LOCKED_FACTS.IDENTITY_STATUS=CONFIRMED` 用于已确认实体；Main 可据此区分“可逐字转述的身份”和“禁止自行猜身份”。
- AnimeTrace 中文名定向搜索优化：对 `ブルーアーカイブ -Blue Archive-` 这类日文+拉丁文作品字段优先抽取稳定的 Latin title 进行查询，提高中文名/中文作品名命中率。

## v0.3.0

- AnimeTrace 候选改为不可变专业视觉证据：原始 `canonical_name / rank / not_confident` 不再经过 Search Synth 删除、替换或重排。
- 引入 AnimeTrace Fast Path：有候选时直接进入 Vision Verify；`not_confident=true` 最多只做 1 次 rank1 定向网页核验，不再执行通用 3 次 Search + Search Synth。
- 只有 AnimeTrace 无匹配、限流或不可用时才进入 Tavily discovery + Search Synth fallback。
- 明确 `not_confident=true` 只是“需要进一步视觉核验”，不是 rank1 错误信号。
- 网页未描述某个外观细节不再视为冲突；只有明确相反事实才进入 HARD_CONFLICT。
- 新增 AnimeTrace 中文名解析：保留 `canonical_name`，可靠找到中文名时增加 `display_name / aliases_zh`；禁止无来源自行音译。
- 新增候选中文名/作品核验缓存，默认 24 小时，降低重复 Tavily 成本。
- Final Vision 固定 `requested_subtasks`，避免 identity + OCR + meme 混合任务在第二阶段漂移成单一 meme 任务。
- Final Vision 输出 identity / OCR / meme 分项置信度；整体 confidence 取最低请求子任务置信度。
- Final Reviewer 增加 `OVERCAUTIOUS` 检测：AnimeTrace rank1 无硬冲突却被无必要弃权时，不调用第三次 Vision，改为以中等置信度保留最可能候选。
- 增加 `LOCKED_FACTS`（IDENTITY_EXACT / IDENTITY_CANONICAL / WORK_EXACT / OCR_EXACT），用于 Main 逐字转述关键事实。
- 修正视觉 follow-up 缓存中的纠错候选提取：优先缓存具体 canonical identity，而不是整段 final answer。

## v0.2.0

- 引入 AnimeTrace / AnimeDB 专用动漫、Galgame、二次元角色识别 grounding。
- 对具体实体身份默认强制 grounding，不再允许 Primary Vision 仅凭模型记忆高置信 FINAL。
- AnimeTrace 429 / 17702 / 17728 / 17731 / 维护状态实现冷却熔断。
- AnimeTrace 全局串行 + 最小调用间隔 + 同图 SHA256 TTL 缓存。
- AnimeTrace 不可用时自动降级为 Tavily grounding。
- 增加 grounded-candidate 约束，Final Vision 不能凭空创造候选列表外的新身份。
- 增加同用户视觉 follow-up 图片缓存。

## v0.1.0

- 初版 Vision -> Search -> Vision 状态机。
