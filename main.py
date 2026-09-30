from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import mimetypes
import os
import re
import time
import urllib.parse
import tempfile
from pathlib import Path
from typing import Any

import aiohttp
from PIL import Image as PILImage, ImageDraw, ImageOps

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Reply
from astrbot.api.star import Context, Star
from astrbot.core.star.star_tools import StarTools
from astrbot.core.astr_agent_context import AgentContextWrapper, AstrAgentContext

from .prompts import (
    ANIMETRACE_VALIDATE_SYSTEM,
    FINAL_REVIEW_SYSTEM,
    PRIMARY_VISION_SYSTEM,
    SEARCH_PLAN_SYSTEM,
    SEARCH_SYNTH_SYSTEM,
    HYBRID_SEARCH_SYNTH_SYSTEM,
    MULTI_REGION_VERIFY_SYSTEM,
    MULTI_REGION_SEARCH_PLAN_SYSTEM,
    MULTI_REGION_SEARCH_SYNTH_SYSTEM,
    VERIFY_VISION_SYSTEM,
)


ANIMETRACE_SUCCESS_CODES = {0, 17720, 200, 17721}
ANIMETRACE_ERRORS = {
    17701: "图片大小过大",
    17702: "服务器繁忙",
    17703: "请求参数不正确",
    17704: "API维护中",
    17705: "图片格式不支持",
    17706: "识别无法完成",
    17707: "内部错误",
    17708: "图片中的人物数量超过限制",
    17722: "图片下载失败",
    17728: "已达到本次使用上限",
    17731: "服务利用人数过多",
    404: "页面不存在",
}
ANIMETRACE_TEMPORARY_CODES = {17702, 17731}
ANIMETRACE_QUOTA_CODES = {17728}
ANIMETRACE_MAINTENANCE_CODES = {17704}


class VisionPipelinePlugin(Star):
    """Deterministic vision workflow exposed as one high-level LLM tool."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # Last locked identity cache only. Images are deliberately NOT persisted/reused.
        # If the current tool call does not carry an image (or an explicit reply image),
        # the pipeline must ask the user to send/attach the image again.
        self._last_by_user: dict[str, dict[str, Any]] = {}

        # AnimeTrace state.
        self._http_session: aiohttp.ClientSession | None = None
        self._animetrace_lock = asyncio.Lock()
        self._animetrace_last_call_at = 0.0
        self._animetrace_cooldown_until = 0.0
        self._animetrace_cooldown_reason = ""
        self._animetrace_cache: dict[str, dict[str, Any]] = {}
        # Candidate localization / validation cache. Keyed by canonical name + work.
        self._animetrace_name_cache: dict[str, dict[str, Any]] = {}
        self._animetrace_models: list[dict[str, Any]] = []
        self._animetrace_model_cache_at = 0.0

        # Lightweight source-universe cache for multi-region/entity-ecosystem discovery.
        # This intentionally stores only names/types/works, never long webpages or full model traces.
        self._source_index_cache: dict[str, dict[str, Any]] = {}
        self._source_index_cache_path: Path | None = None

    async def initialize(self):
        await self._ensure_http_session()
        try:
            self._source_index_cache_path = StarTools.get_data_dir("astrbot_plugin_vision_pipeline") / "source_index_cache.json"
            self._load_source_index_cache()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[vision-pipeline] source index cache init failed; using memory only: %s", exc)
        logger.info("Vision Pipeline v0.7.0 loaded (progressive source discovery + compact region candidate grounding + typed multi-region verify)")

    async def terminate(self):
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        self._http_session = None

    # -------------------- public LLM tool --------------------

    @filter.llm_tool(name="run_vision_pipeline")
    async def run_vision_pipeline(
        self,
        event: AstrMessageEvent,
        task: str = "",
        rejected_candidates: list[str] | None = None,
    ):
        """执行完整视觉流水线。仅处理当前消息中可直接取得的图片，或当前 Reply 组件明确携带的图片；不会从历史消息、旧缓存或 temp 目录猜图。支持角色/实体识别、OCR、UI/游戏截图、表情包、多人物/多物品/货架陈列、图片比较等。AnimeTrace 只作为可选二次元候选源，限流/容量失败/低置信时自动降级。

        Args:
            task(string): 用户当前真正希望从图片中得到的答案。保持用户原意，不要自行添加作品、角色或视觉特征猜测。
            rejected_candidates(array[string]): 仅填写用户本人明确否定的旧候选；不得加入模型/搜索阶段自己排除或讨论过的候选。插件会再次做用户侧来源校验。
        """
        task = (task or event.message_str or "分析当前图片并回答用户的问题").strip()
        cache_key = self._user_cache_key(event)
        # rejected_candidates is an LLM-supplied argument, so never trust it as provenance by itself.
        # Only keep names the current user explicitly mentioned/rejected, or the last locked identity
        # when the current message is clearly a correction. This prevents Main from accidentally
        # blacklisting candidates that only appeared inside an earlier model/tool trace.
        rejected = self._sanitize_user_rejections(
            event=event,
            cache_key=cache_key,
            proposed=self._clean_list(rejected_candidates or []),
        )

        if not rejected and self._looks_like_correction(event.message_str or task):
            cached = self._last_by_user.get(cache_key) or {}
            old = str(cached.get("identity_canonical") or cached.get("identity_exact") or "").strip()
            if old and not self._is_unknown_answer(old):
                rejected = [old]

        try:
            result = await self._run_pipeline(event, task, rejected)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[vision-pipeline] pipeline failed: %s", exc)
            return (
                "VISION_PIPELINE_RESULT\n"
                "STATUS: ERROR\n"
                f"FINAL_ANSWER: 视觉流水线执行失败：{type(exc).__name__}\n"
                "CONFIDENCE: low"
            )

        result.pop("_images", None)
        self._last_by_user[cache_key] = {
            "final_answer": result.get("final_answer", ""),
            "identity_canonical": str((result.get("locked_facts") or {}).get("IDENTITY_CANONICAL") or ""),
            "identity_exact": str((result.get("locked_facts") or {}).get("IDENTITY_EXACT") or ""),
            "result": result,
            "timestamp": time.monotonic(),
        }
        return self._format_result(result)

    # -------------------- direct test commands --------------------

    @filter.command("vpipe")
    async def vpipe(self, event: AstrMessageEvent):
        """直接测试视觉流水线。发送 /vpipe <问题> 并附图或回复图片。"""
        task = (event.message_str or "").partition(" ")[2].strip()
        if not task:
            task = "识别并解释当前图片"
        result = await self._run_pipeline(event, task, [])
        result.pop("_images", None)
        yield event.plain_result(self._format_result(result))

    @filter.command("vpipe_status")
    async def vpipe_status(self, event: AstrMessageEvent):
        """显示流水线、Tavily 与 AnimeTrace 状态，不输出密钥。"""
        cfg = self.context.get_config(umo=event.unified_msg_origin)
        ps = cfg.get("provider_settings", {})
        keys = ps.get("websearch_tavily_key", [])
        if isinstance(keys, str):
            keys = [keys] if keys else []
        cooldown = max(0, int(self._animetrace_cooldown_until - time.monotonic()))
        lines = [
            "Vision Pipeline status",
            f"vision_provider_id: {self.config.get('vision_provider_id') or '(current provider fallback)'}",
            f"search_provider_id: {self.config.get('search_provider_id') or '(current provider fallback)'}",
            f"reviewer_enabled: {bool(self.config.get('enable_reviewer', True))}",
            f"reviewer_provider_id: {self.config.get('reviewer_provider_id') or '(search provider)'}",
            f"force_grounding_entity_identity: {bool(self.config.get('force_grounding_entity_identity', True))}",
            f"max_search_queries: {self._max_search_queries()}",
            f"native_web_search_enabled: {bool(ps.get('web_search', False))}",
            f"native_web_search_provider: {ps.get('websearch_provider', '')}",
            f"tavily_key_count: {len(keys)}",
            f"animetrace_enabled: {bool(self.config.get('animetrace_enabled', True))}",
            f"animetrace_trigger_mode: {self.config.get('animetrace_trigger_mode', 'auto')}",
            f"animetrace_model_id: {self.config.get('animetrace_model_id') or '(dynamic/default)'}",
            f"animetrace_cooldown_remaining_seconds: {cooldown}",
            f"animetrace_cooldown_reason: {self._animetrace_cooldown_reason or '(none)'}",
            f"animetrace_cache_entries: {len(self._animetrace_cache)}",
            f"animetrace_resolve_chinese_names: {bool(self.config.get('animetrace_resolve_chinese_names', True))}",
            f"animetrace_rank1_fallback_medium: {bool(self.config.get('animetrace_rank1_fallback_medium', True))}",
            f"animetrace_name_cache_entries: {len(self._animetrace_name_cache)}",
            f"reviewer_timeout_seconds: {self._reviewer_timeout()}",
            f"search_worker_timeout_seconds: {self._search_worker_timeout()}",
            f"image_policy: current-message-or-explicit-reply-only",
            f"multi_region_enabled: {bool(self.config.get('multi_region_enabled', True))}",
            f"multi_region_contact_sheet: {bool(self.config.get('multi_region_contact_sheet', True))}",
            f"animetrace_multi_object_enabled: {bool(self.config.get('animetrace_multi_object_enabled', False))}",
            f"multi_region_search_planner: {bool(self.config.get('multi_region_search_planner', True))}",
            f"multi_region_candidate_synth: {bool(self.config.get('multi_region_candidate_synth', True))}",
            f"source_index_cache_entries: {len(self._source_index_cache)}",
        ]
        yield event.plain_result("\n".join(lines))

    # -------------------- state machine --------------------

    async def _run_pipeline(
        self,
        event: AstrMessageEvent,
        task: str,
        rejected_candidates: list[str],
    ) -> dict[str, Any]:
        # Deliberately current-message-only. We do not wait for a future image, reuse
        # a historical image cache, inspect temp directories, or guess which old image the user meant.
        images = await self._collect_images(event)
        requested_subtasks = self._requested_subtasks(task)

        if not images:
            locked = {"IMAGE_STATUS": "REQUIRED"}
            if "identity" in requested_subtasks:
                locked["IDENTITY_STATUS"] = "UNCONFIRMED"
            return {
                "status": "FINAL",
                "final_answer": "我目前没有收到这次任务可直接读取的图片。请把图片和问题一起发送，或直接引用一条仍携带图片附件的消息。",
                "confidence": "low",
                "subtask_confidence": {},
                "locked_facts": locked,
                "evidence": [],
                "uncertainty": "当前 Tool Call 没有图片输入；不会使用历史图片或 temp 目录中的旧文件进行猜测。",
                "sources": [],
                "_images": [],
                "meta": self._meta(0, 0, 0, False, 0, 0, False, "image_required"),
            }

        max_images = max(1, int(self.config.get("max_images", 2) or 2))
        images = images[:max_images]
        vision_provider = await self._provider_id(event, "vision_provider_id")
        search_provider = await self._provider_id(event, "search_provider_id")
        reviewer_provider = str(self.config.get("reviewer_provider_id") or "").strip() or search_provider

        vision_calls = 0
        search_calls = 0
        reviewer_calls = 0
        search_blocked = False
        animetrace_calls = 0
        animetrace_cache_hits = 0
        animetrace_blocked = False
        grounding_path = "none"

        # STATE 1: VISION_PRIMARY. requested_subtasks is owned by code and never changes later.
        primary_prompt = self._primary_user_prompt(task, rejected_candidates, requested_subtasks)
        primary, raw = await self._llm_json(
            vision_provider,
            PRIMARY_VISION_SYSTEM,
            primary_prompt,
            image_urls=images,
        )
        vision_calls += 1
        primary = self._normalize_primary(primary, raw)

        is_identity = "identity" in requested_subtasks
        if is_identity:
            primary["task_type"] = "entity_identity"
        self._debug("VISION_PRIMARY", {"requested_subtasks": requested_subtasks, **primary})

        multi_object_mode = self._is_multi_object_task(task, primary)
        if multi_object_mode and bool(self.config.get("multi_region_enabled", True)):
            return await self._run_multi_region_pipeline(
                event=event,
                task=task,
                requested_subtasks=requested_subtasks,
                rejected_candidates=rejected_candidates,
                images=images,
                primary=primary,
                vision_provider=vision_provider,
                search_provider=search_provider,
                counters=(
                    vision_calls, search_calls, reviewer_calls, search_blocked,
                    animetrace_calls, animetrace_cache_hits, animetrace_blocked, "multi_region",
                ),
            )

        # Non-identity tasks remain a one-Vision fast path.
        if not is_identity:
            result = self._final_from_primary(primary, requested_subtasks)
            result["_images"] = images
            result["meta"] = self._meta(
                vision_calls, search_calls, reviewer_calls, search_blocked,
                animetrace_calls, animetrace_cache_hits, animetrace_blocked, grounding_path,
            )
            return result

        # Optional legacy bypass. Default remains forced grounding.
        if not bool(self.config.get("force_grounding_entity_identity", True)):
            if primary.get("status") == "FINAL" and primary.get("confidence") == "high":
                result = self._final_from_primary(primary, requested_subtasks)
                result["_images"] = images
                result["meta"] = self._meta(
                    vision_calls, search_calls, reviewer_calls, search_blocked,
                    animetrace_calls, animetrace_cache_hits, animetrace_blocked, "primary_only",
                )
                return result

        # STATE 2: SPECIALIZED GROUNDING. AnimeTrace candidates are immutable evidence.
        anime_state: dict[str, Any] | None = None
        if self._should_use_animetrace(primary, task):
            anime_state = await self._animetrace_recognize(images[0], rejected_candidates)
            animetrace_calls += int(anime_state.get("api_calls") or 0)
            animetrace_cache_hits += int(bool(anime_state.get("cache_hit")))
            animetrace_blocked = bool(anime_state.get("blocked"))
            self._debug("ANIMETRACE", anime_state)

            anime_candidates = [dict(x) for x in (anime_state.get("candidates") or [])]
            if anime_candidates:
                grounding_path = "animetrace"
                web_validation: dict[str, Any] = {}
                strong_anchors = self._strong_text_anchors(primary)

                # If the image contains a strong copyright/studio/logo/title anchor, do NOT let
                # a low-confidence AnimeTrace hit suppress that text evidence. Run a small hybrid
                # discovery search and append web candidates after the immutable AnimeTrace list.
                if strong_anchors and bool(self.config.get("strong_text_anchor_search", True)):
                    web_candidates, recommendation, used_search, blocked = await self._hybrid_text_grounding(
                        event=event,
                        task=task,
                        requested_subtasks=requested_subtasks,
                        rejected_candidates=rejected_candidates,
                        primary=primary,
                        anime_candidates=anime_candidates,
                        search_provider=search_provider,
                    )
                    search_calls += used_search
                    search_blocked = search_blocked or blocked
                    merged = self._merge_candidates_preserve_order(anime_candidates, web_candidates)
                    grounding_path = "animetrace_plus_text_anchor" if web_candidates else "animetrace_text_anchor_no_web_candidate"
                    return await self._verify_and_finish(
                        event=event,
                        task=task,
                        requested_subtasks=requested_subtasks,
                        rejected_candidates=rejected_candidates,
                        images=images,
                        primary=primary,
                        candidates=merged,
                        web_validation={"strong_text_anchors": strong_anchors},
                        search_recommendation=recommendation or "强文字锚点与 AnimeTrace 候选需交叉核验。",
                        candidate_source="AnimeTrace + Text Anchor Search",
                        vision_provider=vision_provider,
                        reviewer_provider=reviewer_provider,
                        allow_animetrace_rank1_guard=False,
                        counters=(
                            vision_calls, search_calls, reviewer_calls, search_blocked,
                            animetrace_calls, animetrace_cache_hits, animetrace_blocked, grounding_path,
                        ),
                    )

                need_targeted_web = bool(
                    (anime_state.get("not_confident") and self.config.get("animetrace_web_enrich_when_uncertain", True))
                    or self.config.get("animetrace_resolve_chinese_names", True)
                )

                if need_targeted_web:
                    validation, used_search, blocked = await self._animetrace_validate_rank1(
                        event=event,
                        search_provider=search_provider,
                        candidates=anime_candidates,
                    )
                    search_calls += used_search
                    search_blocked = search_blocked or blocked
                    web_validation = validation
                    if validation:
                        grounding_path = "animetrace_plus_targeted_web"
                        anime_candidates = self._enrich_animetrace_candidates(
                            anime_candidates,
                            validation,
                        )
                        self._debug("ANIMETRACE_WEB_VALIDATE", validation)

                return await self._verify_and_finish(
                    event=event,
                    task=task,
                    requested_subtasks=requested_subtasks,
                    rejected_candidates=rejected_candidates,
                    images=images,
                    primary=primary,
                    candidates=anime_candidates,
                    web_validation=web_validation,
                    search_recommendation=(
                        "AnimeTrace 是辅助候选源，不是不可挑战的真理。"
                        "not_confident=true 时不得使用代码级 rank1 强制保底；必须以原图最终核验。"
                    ),
                    candidate_source="AnimeTrace",
                    vision_provider=vision_provider,
                    reviewer_provider=reviewer_provider,
                    allow_animetrace_rank1_guard=not bool(anime_state.get("not_confident")),
                    counters=(
                        vision_calls, search_calls, reviewer_calls, search_blocked,
                        animetrace_calls, animetrace_cache_hits, animetrace_blocked, grounding_path,
                    ),
                )

        # STATE 3: GENERIC WEB FALLBACK. Only used when AnimeTrace gave no candidate / was unavailable.
        queries = self._initial_search_queries(primary, [], rejected_candidates)
        if not queries:
            try:
                plan, _ = await asyncio.wait_for(
                    self._llm_json(
                        search_provider,
                        SEARCH_PLAN_SYSTEM,
                        json.dumps(
                            {
                                "task": task,
                                "requested_subtasks": requested_subtasks,
                                "rejected_candidates": rejected_candidates,
                                "primary": self._compact_primary(primary),
                                "max_queries": self._max_search_queries(),
                            },
                            ensure_ascii=False,
                        ),
                    ),
                    timeout=min(20, self._search_worker_timeout()),
                )
                queries = self._clean_list(plan.get("queries", []))[: self._max_search_queries()]
                self._debug("SEARCH_PLAN", {"queries": queries})
            except asyncio.TimeoutError:
                logger.warning("[vision-pipeline] Search Planner timed out; ending grounding without extra LLM retries")
                queries = []

        search_results: list[dict[str, Any]] = []
        for query in queries[: self._max_search_queries()]:
            try:
                text = await self._native_tavily_search(event, query)
                search_calls += 1
            except Exception as exc:  # noqa: BLE001
                if self._is_rate_limited(str(exc)):
                    search_blocked = True
                    break
                logger.warning("[vision-pipeline] Tavily search failed: %s", exc)
                continue
            if self._is_rate_limited(text):
                search_blocked = True
                break
            compact = self._compact_search_text(text)
            if compact:
                search_results.append({"query": query, "result": compact})

        if not search_results:
            result = {
                "status": "FINAL",
                "final_answer": "角色身份：无法可靠确认。",
                "confidence": "low",
                "subtask_confidence": {"identity": "low"},
                "locked_facts": {"IDENTITY_STATUS": "UNCONFIRMED", "OCR_EXACT": primary.get("ocr", "")},
                "evidence": self._clean_list(primary.get("certain_observations", []))[:4],
                "uncertainty": (
                    "联网检索受到限流/配额限制。" if search_blocked
                    else ((anime_state or {}).get("reason") or "未检索到有效具名候选。")
                ),
                "sources": [],
                "_images": images,
                "meta": self._meta(
                    vision_calls, search_calls, reviewer_calls, search_blocked,
                    animetrace_calls, animetrace_cache_hits, animetrace_blocked, "no_candidates",
                ),
            }
            return result

        grounding_path = "web"
        synth_input = {
            "task": task,
            "requested_subtasks": requested_subtasks,
            "rejected_candidates": rejected_candidates,
            "primary": self._compact_primary(primary),
            "search_results": search_results,
        }
        try:
            search_state, _ = await asyncio.wait_for(
                self._llm_json(
                    search_provider,
                    SEARCH_SYNTH_SYSTEM,
                    json.dumps(synth_input, ensure_ascii=False),
                ),
                timeout=self._search_worker_timeout(),
            )
            search_state = self._normalize_search_state(search_state)
            self._debug("SEARCH_SYNTH", search_state)
        except asyncio.TimeoutError:
            logger.warning("[vision-pipeline] Search Synth timed out; returning unconfirmed instead of risking outer tool timeout")
            search_state = {
                "status": "SEARCH_COMPLETE",
                "candidates": [],
                "recommendation": "Search Synth 超时；未形成可安全使用的具名候选。",
                "extract_url": "",
            }

        if self.config.get("enable_extract", False):
            extract_url = str(search_state.get("extract_url") or "").strip()
            if extract_url and not search_blocked:
                try:
                    extract_text = await self._native_tavily_extract(event, extract_url)
                    if self._is_rate_limited(extract_text):
                        search_blocked = True
                    else:
                        synth_input["extracted_page"] = self._truncate(
                            extract_text,
                            int(self.config.get("max_extract_chars", 5000) or 5000),
                        )
                        try:
                            search_state, _ = await asyncio.wait_for(
                                self._llm_json(
                                    search_provider,
                                    SEARCH_SYNTH_SYSTEM,
                                    json.dumps(synth_input, ensure_ascii=False),
                                ),
                                timeout=self._search_worker_timeout(),
                            )
                            search_state = self._normalize_search_state(search_state)
                        except asyncio.TimeoutError:
                            logger.warning("[vision-pipeline] Search Synth after extract timed out; keeping pre-extract candidates")
                except Exception as exc:  # noqa: BLE001
                    if self._is_rate_limited(str(exc)):
                        search_blocked = True
                    else:
                        logger.warning("[vision-pipeline] Tavily extract failed: %s", exc)

        candidates = search_state.get("candidates") or []
        if not candidates:
            result = {
                "status": "FINAL",
                "final_answer": "角色身份：无法可靠确认。",
                "confidence": "low",
                "subtask_confidence": {"identity": "low"},
                "locked_facts": {"IDENTITY_STATUS": "UNCONFIRMED", "OCR_EXACT": primary.get("ocr", "")},
                "evidence": self._clean_list(primary.get("certain_observations", []))[:4],
                "uncertainty": str(search_state.get("recommendation") or "grounding 阶段没有留下合理具名候选。"),
                "sources": [],
                "_images": images,
                "meta": self._meta(
                    vision_calls, search_calls, reviewer_calls, search_blocked,
                    animetrace_calls, animetrace_cache_hits, animetrace_blocked, grounding_path,
                ),
            }
            return result

        return await self._verify_and_finish(
            event=event,
            task=task,
            requested_subtasks=requested_subtasks,
            rejected_candidates=rejected_candidates,
            images=images,
            primary=primary,
            candidates=candidates[:3],
            web_validation={},
            search_recommendation=search_state.get("recommendation", ""),
            candidate_source="Web Search",
            vision_provider=vision_provider,
            reviewer_provider=reviewer_provider,
            allow_animetrace_rank1_guard=False,
            counters=(
                vision_calls, search_calls, reviewer_calls, search_blocked,
                animetrace_calls, animetrace_cache_hits, animetrace_blocked, grounding_path,
            ),
        )


    async def _run_multi_region_pipeline(
        self,
        *,
        event: AstrMessageEvent,
        task: str,
        requested_subtasks: list[str],
        rejected_candidates: list[str],
        images: list[str],
        primary: dict[str, Any],
        vision_provider: str,
        search_provider: str,
        counters: tuple[int, int, int, bool, int, int, bool, str],
    ) -> dict[str, Any]:
        """One pipeline, many regions, with progressive retrieval.

        The expensive Vision model is used once for primary perception and once for final
        visual verification. Search is deliberately compact: discover the source ecosystem
        only on cache miss, then search region-specific visual traits. We never seed discovery
        with Primary's guessed names because that caused candidate lock-in in earlier versions.
        """
        (
            vision_calls, search_calls, reviewer_calls, search_blocked,
            animetrace_calls, animetrace_cache_hits, animetrace_blocked, grounding_path,
        ) = counters

        regions = list(primary.get("regions") or [])[: self._multi_region_max_regions()]
        strong_anchors = self._strong_text_anchors(primary)
        source_key = self._source_cache_key(strong_anchors)
        cached_source_index = self._source_cache_get(source_key)
        search_results: list[dict[str, Any]] = []
        candidate_grounding: dict[str, Any] = {
            "source_index": cached_source_index or {},
            "regions": [],
            "global_notes": [],
        }

        # Progressive search plan. Primary candidate guesses are intentionally NOT passed.
        # Cache miss: at most two searches (source-universe + region traits).
        # Cache hit: at most one region-traits search.
        queries: list[str] = []
        if bool(self.config.get("strong_text_anchor_search", True)) and (
            strong_anchors or primary.get("status") == "NEED_SEARCH"
        ):
            if bool(self.config.get("multi_region_search_planner", True)):
                plan_payload = json.dumps(
                    {
                        "task": task,
                        "rejected_candidates": rejected_candidates,
                        "strong_text_anchors": strong_anchors,
                        "source_index_cache": cached_source_index or {},
                        "regions": self._regions_for_search(regions),
                        "cache_hit": bool(cached_source_index),
                    },
                    ensure_ascii=False,
                )
                try:
                    plan_data, _ = await asyncio.wait_for(
                        self._llm_json(search_provider, MULTI_REGION_SEARCH_PLAN_SYSTEM, plan_payload),
                        timeout=self._search_worker_timeout(),
                    )
                    queries = self._clean_list(plan_data.get("queries", []))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[vision-pipeline] multi-region search planner failed: %s", exc)

            if not queries:
                queries = self._multi_region_search_queries(primary, cached_source_index)

            budget = self._multi_region_max_search_queries()
            if cached_source_index:
                budget = min(budget, 1)
            for query in queries[:budget]:
                try:
                    text = await self._native_tavily_search(event, query)
                    search_calls += 1
                except Exception as exc:  # noqa: BLE001
                    if self._is_rate_limited(str(exc)):
                        search_blocked = True
                        break
                    logger.warning("[vision-pipeline] multi-region Tavily failed: %s", exc)
                    continue
                if self._is_rate_limited(text):
                    search_blocked = True
                    break
                compact = self._compact_search_text(text, max_items=3, snippet_chars=420)
                if compact:
                    search_results.append({"query": query[:240], "result": compact})

        # One cheap synth turns snippets into compact candidate cards. Its output is intentionally
        # small and typed; long prose is discarded during normalization.
        if (search_results or cached_source_index) and bool(self.config.get("multi_region_candidate_synth", True)):
            synth_payload = json.dumps(
                {
                    "task": task,
                    "rejected_candidates": rejected_candidates,
                    "strong_text_anchors": strong_anchors,
                    "source_index_cache": cached_source_index or {},
                    "regions": self._regions_for_search(regions),
                    "search_results": search_results,
                },
                ensure_ascii=False,
            )
            try:
                synth_raw, _ = await asyncio.wait_for(
                    self._llm_json(search_provider, MULTI_REGION_SEARCH_SYNTH_SYSTEM, synth_payload),
                    timeout=self._search_worker_timeout(),
                )
                candidate_grounding = self._normalize_multi_candidate_grounding(synth_raw, regions)
                if cached_source_index and not candidate_grounding.get("source_index"):
                    candidate_grounding["source_index"] = copy.deepcopy(cached_source_index)
                source_index = candidate_grounding.get("source_index") or {}
                if source_key and source_index:
                    self._source_cache_put(source_key, source_index)
                self._debug("MULTI_REGION_GROUNDING", candidate_grounding)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[vision-pipeline] multi-region candidate synth failed: %s", exc)

        anime_state: dict[str, Any] = {}
        if bool(self.config.get("animetrace_multi_object_enabled", False)) and self._should_use_animetrace(primary, task):
            anime_state = await self._animetrace_recognize(images[0], rejected_candidates)
            animetrace_calls += int(anime_state.get("api_calls") or 0)
            animetrace_cache_hits += int(bool(anime_state.get("cache_hit")))
            animetrace_blocked = bool(anime_state.get("blocked"))
            self._debug("ANIMETRACE_MULTI_AUX", anime_state)

        verify_images = list(images[:1])
        contact_path = ""
        if bool(self.config.get("multi_region_contact_sheet", True)) and len(regions) >= 2:
            try:
                contact_path = await self._build_contact_sheet(images[0], regions)
                if contact_path:
                    verify_images = [contact_path]
                    grounding_path = "multi_region_contact_sheet"
            except Exception as exc:  # noqa: BLE001
                logger.warning("[vision-pipeline] contact sheet build failed: %s", exc)

        # Do not feed raw web snippets into the expensive final Vision model. Candidate cards plus
        # a tiny source index are enough; this is the main v0.7 token-saving change.
        verify_prompt = json.dumps(
            {
                "task": task,
                "requested_subtasks": requested_subtasks,
                "rejected_candidates": rejected_candidates,
                "primary_visual_facts": {
                    "ocr": str(primary.get("ocr") or "")[:240],
                    "strong_text_anchors": strong_anchors,
                    "regions": self._regions_for_search(regions),
                },
                "grounded_entity_candidates": candidate_grounding,
                "animetrace_auxiliary": {
                    "status": anime_state.get("status"),
                    "not_confident": anime_state.get("not_confident"),
                    "reason": anime_state.get("reason", ""),
                    "candidates": [self._candidate_for_prompt(x) for x in (anime_state.get("candidates") or [])[:6]],
                },
                "instruction": (
                    "一次性识别所有 region。先判断视觉身份，再单独记录 related_entities。"
                    "候选池不是强制二选一；若都不符合，decision 必须是 NONE_OF_ABOVE/UNRESOLVED。"
                    "只有候选卡里存在可核验 visual_traits，且原图至少两项独特特征吻合时才允许 high。"
                    "来源版权只能限定生态，不能自动等于具体作品。"
                ),
            },
            ensure_ascii=False,
        )
        try:
            multi_raw, raw = await self._llm_json(
                vision_provider,
                MULTI_REGION_VERIFY_SYSTEM,
                verify_prompt,
                image_urls=verify_images,
            )
            vision_calls += 1
            multi = self._normalize_multi_result(multi_raw, raw, regions, primary, candidate_grounding)
            self._debug("MULTI_REGION_VERIFY", multi)
        finally:
            if contact_path:
                try:
                    Path(contact_path).unlink(missing_ok=True)
                except Exception:
                    pass

        entities = multi.get("entities") if isinstance(multi.get("entities"), list) else []
        if bool(self.config.get("multi_region_strict_typed_answer", True)):
            final_answer = self._render_multi_typed_answer(entities, str((multi.get("ocr") or {}).get("text") or ""))
        else:
            final_answer = str(multi.get("overall_answer") or "").strip()
        if not final_answer:
            final_answer = self._render_multi_typed_answer(entities, str((multi.get("ocr") or {}).get("text") or ""))
        if not final_answer:
            final_answer = "无法可靠确认图中各对象的具体身份。"

        sub_conf = {"multi_object": self._normalize_confidence(multi.get("confidence"))}
        ocr_text = str((multi.get("ocr") or {}).get("text") or primary.get("ocr") or "").strip()
        locked: dict[str, str] = {"IDENTITY_STATUS": "MULTI"}
        if ocr_text:
            locked["OCR_EXACT"] = ocr_text
        for item in entities:
            rid = re.sub(r"[^A-Za-z0-9_-]", "", str(item.get("region_id") or ""))[:8]
            name = str(item.get("display_name") or item.get("visual_identity") or "").strip()
            if rid and name and not self._is_unknown_answer(name):
                locked[f"ENTITY_{rid}_EXACT"] = name
                canonical = str(item.get("canonical_name") or "").strip()
                if canonical:
                    locked[f"ENTITY_{rid}_CANONICAL"] = canonical
                etype = str(item.get("entity_type") or "").strip()
                if etype:
                    locked[f"ENTITY_{rid}_TYPE"] = etype
                work = str(item.get("work") or "").strip()
                if work:
                    locked[f"ENTITY_{rid}_WORK"] = work

        sources = self._clean_list(multi.get("sources", []))[:4]
        if anime_state.get("candidates") and not sources:
            sources.append(f"AnimeTrace ({anime_state.get('model') or 'service-default'})")

        return {
            "status": "FINAL",
            "final_answer": final_answer,
            "confidence": self._normalize_confidence(multi.get("confidence")),
            "subtask_confidence": sub_conf,
            "locked_facts": locked,
            "evidence": self._clean_list(multi.get("evidence", []))[:6],
            "uncertainty": str(multi.get("uncertainty") or "").strip(),
            "sources": sources,
            "meta": self._meta(
                vision_calls, search_calls, reviewer_calls, search_blocked,
                animetrace_calls, animetrace_cache_hits, animetrace_blocked,
                grounding_path or "multi_region",
            ),
        }

    def _source_cache_ttl(self) -> int:
        try:
            return max(300, min(30 * 86400, int(self.config.get("source_index_cache_ttl_seconds", 604800) or 604800)))
        except Exception:
            return 604800

    def _source_cache_key(self, anchors: list[str]) -> str:
        if not anchors:
            return ""
        text = " ".join(anchors[:2]).upper()
        text = re.sub(r"©|COPYRIGHT", " ", text)
        text = re.sub(r"\b20\d{2}\b", " ", text)
        text = re.sub(r"\b(?:INC|LTD|CO|CORP|CORPORATION)\.?\b", " ", text)
        text = re.sub(r"[^0-9A-Z\u3040-\u30ff\u3400-\u9fff]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()[:180]

    def _load_source_index_cache(self) -> None:
        path = self._source_index_cache_path
        if not path or not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            now = time.time()
            if isinstance(raw, dict):
                self._source_index_cache = {
                    str(k): v for k, v in raw.items()
                    if isinstance(v, dict) and float(v.get("expires_at") or 0) > now and isinstance(v.get("value"), dict)
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("[vision-pipeline] failed to load source index cache: %s", exc)

    def _persist_source_index_cache(self) -> None:
        path = self._source_index_cache_path
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Keep the cache deliberately small; it is an index, not a knowledge base.
            rows = sorted(
                self._source_index_cache.items(),
                key=lambda kv: float((kv[1] or {}).get("expires_at") or 0),
                reverse=True,
            )[:64]
            payload = {k: v for k, v in rows}
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[vision-pipeline] failed to persist source index cache: %s", exc)

    def _source_cache_get(self, key: str) -> dict[str, Any]:
        if not key:
            return {}
        row = self._source_index_cache.get(key) or {}
        if not row:
            return {}
        if float(row.get("expires_at") or 0) <= time.time():
            self._source_index_cache.pop(key, None)
            self._persist_source_index_cache()
            return {}
        value = row.get("value")
        return copy.deepcopy(value) if isinstance(value, dict) else {}

    def _source_cache_put(self, key: str, value: dict[str, Any]) -> None:
        if not key or not isinstance(value, dict) or not value:
            return
        self._source_index_cache[key] = {
            "expires_at": time.time() + self._source_cache_ttl(),
            "value": copy.deepcopy(value),
        }
        self._persist_source_index_cache()

    @staticmethod
    def _regions_for_search(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for row in regions[:8]:
            if not isinstance(row, dict):
                continue
            out.append({
                "region_id": str(row.get("region_id") or ""),
                "kind": str(row.get("kind") or "other"),
                "label_hint": str(row.get("label_hint") or "")[:80],
                "direct_observations": [str(x)[:120] for x in (row.get("direct_observations") or [])[:5]],
                "ocr": str(row.get("ocr") or "")[:120],
            })
        return out

    def _strong_text_anchors(self, primary: dict[str, Any]) -> list[str]:
        anchors = self._clean_list(primary.get("strong_text_anchors", []))[:4]
        texts = [str(primary.get("ocr") or "").strip()]
        for region in primary.get("regions") or []:
            if isinstance(region, dict):
                texts.append(str(region.get("ocr") or "").strip())
        strong_markers = ["©", "STUDIO", "INC.", " INC", "PROJECT", "PRODUCTION", "OFFICIAL", "PRESENTS"]
        for text in texts:
            if not text:
                continue
            upper = text.upper()
            if any(x in upper for x in strong_markers) or re.search(r"\b[A-Z][A-Z0-9&._ -]{4,}\b", text):
                item = text[:160]
                if item not in anchors:
                    anchors.append(item)
            if len(anchors) >= 4:
                break
        return anchors[:4]

    def _is_multi_object_task(self, task: str, primary: dict[str, Any]) -> bool:
        if bool(primary.get("multi_object")):
            return True
        if len(primary.get("regions") or []) >= 2:
            return True
        t = str(task or "").lower()
        patterns = [
            "分别", "两位", "两个人", "多个人", "多人", "有哪些人物", "哪些人物", "所有人物",
            "有哪些东西", "哪些东西", "不同东西", "不同物品", "货架", "柜子", "陈列", "展柜",
            "每个", "逐个", "各个", "一排", "一层", "多件", "multiple", "each item", "all characters",
        ]
        return any(p in t for p in patterns)

    def _multi_region_max_regions(self) -> int:
        try:
            return max(2, min(10, int(self.config.get("multi_region_max_regions", 6) or 6)))
        except Exception:
            return 6

    def _multi_region_max_search_queries(self) -> int:
        try:
            return max(0, min(3, int(self.config.get("multi_region_max_search_queries", 2) or 2)))
        except Exception:
            return 2

    def _multi_region_search_queries(self, primary: dict[str, Any], cached_source_index: dict[str, Any] | None = None) -> list[str]:
        """Deterministic open-discovery fallback.

        Never insert Primary's guessed candidate names. On cache miss, first discover the source
        ecosystem; then search by region-level visual traits. On cache hit, only do the latter.
        """
        limit = self._multi_region_max_search_queries()
        if limit <= 0:
            return []
        anchors = self._strong_text_anchors(primary)
        source = self._source_cache_key(anchors) or (anchors[0][:100] if anchors else "")
        person_regions = [r for r in (primary.get("regions") or []) if str(r.get("kind") or "") in {"person", "character", "object"}]
        traits: list[str] = []
        for row in person_regions[:3]:
            for obs in self._clean_list(row.get("direct_observations", []))[:3]:
                if obs not in traits:
                    traits.append(obs)
        out: list[str] = []
        if not cached_source_index and source:
            out.append(f'{source} 所属 一览 virtual artist 音楽的同位体 キャラクター project character')
        if source and traits:
            short_traits = " ".join(x[:60] for x in traits[:5])
            out.append(f'{source} {short_traits} 角色 キャラクター 立ち絵')
        elif traits:
            out.append(" ".join(x[:70] for x in traits[:5]) + " 角色 キャラクター")
        elif source:
            out.append(f'{source} character list entity types')
        return out[:limit]

    def _normalize_regions(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        out: list[dict[str, Any]] = []
        for idx, item in enumerate(value[:10]):
            if not isinstance(item, dict):
                continue
            box = item.get("box")
            coords: tuple[float, float, float, float] | None = None
            # v0.7 canonical format: named fields eliminate xy/yx ambiguity.
            if isinstance(box, dict):
                try:
                    left = float(box.get("left"))
                    top = float(box.get("top"))
                    right = float(box.get("right"))
                    bottom = float(box.get("bottom"))
                    coords = (left, top, right, bottom)
                except Exception:
                    coords = None
            elif isinstance(box, list) and len(box) == 4:
                # Backward compatibility for older model outputs. Evaluate both xyxy and
                # top-left-bottom-right interpretations, and use label/position hints to choose.
                try:
                    vals = [float(x) for x in box]
                    coords = self._choose_legacy_region_box(vals, str(item.get("label_hint") or ""))
                except Exception:
                    coords = None
            if not coords:
                continue
            vals = list(coords)
            if max(abs(x) for x in vals) <= 1.5:
                vals = [x * 1000.0 for x in vals]
            left, top, right, bottom = [max(0.0, min(1000.0, x)) for x in vals]
            if right <= left or bottom <= top:
                continue
            rid = str(item.get("region_id") or chr(ord("A") + len(out))).strip()[:8]
            out.append(
                {
                    "region_id": rid,
                    "box": {
                        "left": round(left, 1),
                        "top": round(top, 1),
                        "right": round(right, 1),
                        "bottom": round(bottom, 1),
                    },
                    "kind": str(item.get("kind") or "other").strip(),
                    "label_hint": str(item.get("label_hint") or "").strip()[:120],
                    "direct_observations": self._clean_list(item.get("direct_observations", []))[:4],
                    "ocr": str(item.get("ocr") or "").strip()[:200],
                }
            )
        return out

    def _choose_legacy_region_box(self, vals: list[float], label_hint: str) -> tuple[float, float, float, float] | None:
        if len(vals) != 4:
            return None
        if max(abs(x) for x in vals) <= 1.5:
            vals = [x * 1000.0 for x in vals]
        a, b, c, d = vals
        candidates = []
        # xyxy
        if c > a and d > b:
            candidates.append((a, b, c, d))
        # yxyx / top,left,bottom,right
        if d > b and c > a:
            candidates.append((b, a, d, c))
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        hint = label_hint.lower()
        def score(box: tuple[float, float, float, float]) -> float:
            left, top, right, bottom = box
            cx, cy = (left + right) / 2.0, (top + bottom) / 2.0
            sc = 0.0
            if any(x in hint for x in ["左", "left"]): sc += (1000.0 - cx) / 1000.0
            if any(x in hint for x in ["右", "right"]): sc += cx / 1000.0
            if any(x in hint for x in ["上", "顶部", "top"]): sc += (1000.0 - cy) / 1000.0
            if any(x in hint for x in ["下", "底部", "bottom"]): sc += cy / 1000.0
            # Prefer plausible object boxes over extremely thin strips unless explicitly text.
            rw, rh = max(1.0, right-left), max(1.0, bottom-top)
            aspect = min(rw, rh) / max(rw, rh)
            if "text" not in hint and "文字" not in hint:
                sc += min(0.25, aspect * 0.25)
            return sc
        return max(candidates, key=score)

    @staticmethod
    def _region_box_xyxy(reg: dict[str, Any]) -> tuple[float, float, float, float] | None:
        box = reg.get("box")
        if not isinstance(box, dict):
            return None
        try:
            return (float(box["left"]), float(box["top"]), float(box["right"]), float(box["bottom"]))
        except Exception:
            return None

    async def _build_contact_sheet(self, image_ref: str, regions: list[dict[str, Any]]) -> str:
        data, _filename, _ctype = await self._read_image_bytes(image_ref)
        im = ImageOps.exif_transpose(PILImage.open(io.BytesIO(data))).convert("RGB")
        w, h = im.size
        if w <= 0 or h <= 0:
            return ""

        regions = regions[: self._multi_region_max_regions()]
        if not regions:
            return ""

        sheet_w = 1024
        margin = 18
        title_h = 34
        overview_h = min(420, max(240, int(sheet_w * h / max(w, 1) * 0.34)))
        overview = im.copy()
        overview.thumbnail((sheet_w - 2 * margin, overview_h), PILImage.Resampling.LANCZOS)
        cell_w = (sheet_w - 3 * margin) // 2
        cell_h = 360
        rows = (len(regions) + 1) // 2
        sheet_h = margin + title_h + overview.height + margin + rows * (cell_h + margin)
        canvas = PILImage.new("RGB", (sheet_w, sheet_h), "white")
        draw = ImageDraw.Draw(canvas)
        draw.text((margin, 8), "Original overview", fill="black")
        canvas.paste(overview, ((sheet_w - overview.width) // 2, margin + title_h))
        y0 = margin + title_h + overview.height + margin

        pad_x_ratio = max(0.0, min(0.5, float(self.config.get("multi_region_padding_x", 0.12) or 0.12)))
        pad_top_ratio = max(0.0, min(0.7, float(self.config.get("multi_region_padding_top", 0.30) or 0.30)))
        pad_bottom_ratio = max(0.0, min(0.5, float(self.config.get("multi_region_padding_bottom", 0.15) or 0.15)))

        for idx, reg in enumerate(regions):
            coords = self._region_box_xyxy(reg)
            if not coords:
                continue
            left, top, right, bottom = [x / 1000.0 for x in coords]
            x1, y1, x2, y2 = left * w, top * h, right * w, bottom * h
            rw, rh = max(1.0, x2 - x1), max(1.0, y2 - y1)
            x1 -= rw * pad_x_ratio
            x2 += rw * pad_x_ratio
            y1 -= rh * pad_top_ratio
            y2 += rh * pad_bottom_ratio
            crop_box = (max(0, int(x1)), max(0, int(y1)), min(w, int(x2)), min(h, int(y2)))
            if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
                continue
            crop = im.crop(crop_box)
            crop.thumbnail((cell_w - 20, cell_h - 54), PILImage.Resampling.LANCZOS)
            col, row = idx % 2, idx // 2
            cx = margin + col * (cell_w + margin)
            cy = y0 + row * (cell_h + margin)
            draw.rectangle((cx, cy, cx + cell_w, cy + cell_h), outline="black", width=2)
            rid = str(reg.get("region_id") or chr(ord("A") + idx))
            hint = str(reg.get("label_hint") or "")[:40]
            draw.text((cx + 10, cy + 8), f"{rid}: {hint}" if hint else rid, fill="black")
            px = cx + (cell_w - crop.width) // 2
            py = cy + 40 + max(0, (cell_h - 50 - crop.height) // 2)
            canvas.paste(crop, (px, py))

        local_ref = Path(str(image_ref))
        out_dir = local_ref.parent if local_ref.exists() else Path("data/temp")
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            out_dir = Path(tempfile.gettempdir())
        out_path = out_dir / f"vision_pipeline_contact_{int(time.time()*1000)}_{hashlib.md5(data).hexdigest()[:8]}.jpg"
        canvas.save(out_path, format="JPEG", quality=90, optimize=True)
        return str(out_path.resolve())

    def _normalize_multi_candidate_grounding(self, data: dict[str, Any], regions: list[dict[str, Any]]) -> dict[str, Any]:
        valid_ids = {str(x.get("region_id")) for x in regions}
        out_regions: list[dict[str, Any]] = []
        raw_regions = data.get("regions") if isinstance(data.get("regions"), list) else []
        for row in raw_regions:
            if not isinstance(row, dict):
                continue
            rid = str(row.get("region_id") or "").strip()
            if valid_ids and rid not in valid_ids:
                continue
            cands = []
            for cand in (row.get("candidates") if isinstance(row.get("candidates"), list) else [])[:3]:
                if not isinstance(cand, dict):
                    continue
                canonical = str(cand.get("canonical_name") or "").strip()
                if not canonical:
                    continue
                rels = []
                for rel in (cand.get("related_entities") if isinstance(cand.get("related_entities"), list) else [])[:2]:
                    if isinstance(rel, dict) and str(rel.get("name") or "").strip():
                        rels.append({
                            "relation": str(rel.get("relation") or "other").strip(),
                            "name": str(rel.get("name") or "").strip(),
                            "note": str(rel.get("note") or "").strip()[:120],
                        })
                source_url = str(cand.get("source_url") or "").strip()[:1000]
                sources = self._clean_list(cand.get("sources", []))[:1]
                if source_url and source_url not in sources:
                    sources.insert(0, source_url)
                cands.append({
                    "canonical_name": canonical,
                    "display_name": str(cand.get("display_name") or canonical).strip(),
                    "entity_type": str(cand.get("entity_type") or "other").strip(),
                    "work": str(cand.get("work") or "").strip(),
                    "visual_traits": self._clean_list(cand.get("visual_traits", []))[:3],
                    "sources": sources[:1],
                    "related_entities": rels,
                })
            out_regions.append({"region_id": rid, "candidates": cands})

        source_index_raw = data.get("source_index") if isinstance(data.get("source_index"), dict) else {}
        idx_entities = []
        for item in (source_index_raw.get("entities") if isinstance(source_index_raw.get("entities"), list) else [])[:16]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("canonical_name") or item.get("name") or "").strip()
            if not name:
                continue
            idx_entities.append({
                "canonical_name": name,
                "entity_type": str(item.get("entity_type") or "other").strip(),
                "work": str(item.get("work") or "").strip()[:120],
            })
        source_index = {
            "source_name": str(source_index_raw.get("source_name") or "").strip()[:120],
            "entity_types": self._clean_list(source_index_raw.get("entity_types", []))[:8],
            "entities": idx_entities,
        }
        if not source_index["source_name"] and not source_index["entities"]:
            source_index = {}

        return {
            "source_index": source_index,
            "regions": out_regions,
            "global_notes": self._clean_list(data.get("global_notes", []))[:2],
        }

    def _normalize_multi_result(
        self,
        data: dict[str, Any],
        raw: str,
        regions: list[dict[str, Any]],
        primary: dict[str, Any],
        candidate_grounding: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        valid_ids = {str(x.get("region_id")) for x in regions}
        region_kind = {str(x.get("region_id")): str(x.get("kind") or "other") for x in regions}
        candidate_info: dict[str, dict[str, dict[str, Any]]] = {}
        for row in ((candidate_grounding or {}).get("regions") or []):
            if not isinstance(row, dict):
                continue
            rid = str(row.get("region_id") or "")
            candidate_info[rid] = {}
            for cand in (row.get("candidates") or []):
                if not isinstance(cand, dict):
                    continue
                name = str(cand.get("canonical_name") or "").strip()
                if name:
                    candidate_info[rid][name] = cand

        entities: list[dict[str, Any]] = []
        raw_entities = data.get("entities") if isinstance(data.get("entities"), list) else []
        for item in raw_entities[: self._multi_region_max_regions()]:
            if not isinstance(item, dict):
                continue
            rid = str(item.get("region_id") or "").strip()
            if valid_ids and rid not in valid_ids:
                continue
            decision = str(item.get("decision") or "MATCH").strip().upper()
            canonical = str(item.get("canonical_name") or "").strip()
            allowed = candidate_info.get(rid) or {}
            if decision in {"NONE_OF_ABOVE", "UNRESOLVED"}:
                canonical = ""
                visual_identity = "无法可靠确认"
                display_name = ""
                confidence = "low"
            elif allowed and (not canonical or canonical not in allowed):
                canonical = ""
                visual_identity = "无法可靠确认"
                display_name = ""
                confidence = "low"
                decision = "UNRESOLVED"
            else:
                visual_identity = str(item.get("visual_identity") or item.get("identity") or "").strip()
                display_name = str(item.get("display_name") or canonical or visual_identity).strip()
                confidence = self._normalize_confidence(item.get("confidence"))
                decision = "MATCH" if canonical else "UNRESOLVED"

                # Code-level confidence ceiling: relationship-only web grounding is not enough
                # for high-confidence visual identity. Require candidate visual traits plus at
                # least two visual evidence items from Final Vision.
                if canonical and confidence == "high":
                    card = allowed.get(canonical) or {}
                    visual_traits = self._clean_list(card.get("visual_traits", []))
                    final_evidence = self._clean_list(item.get("evidence", []))
                    if not visual_traits or len(final_evidence) < 2:
                        confidence = "medium"

            rels = []
            for rel in (item.get("related_entities") if isinstance(item.get("related_entities"), list) else [])[:3]:
                if isinstance(rel, dict) and str(rel.get("name") or "").strip():
                    rels.append({
                        "relation": str(rel.get("relation") or "other").strip(),
                        "name": str(rel.get("name") or "").strip(),
                        "note": str(rel.get("note") or "").strip()[:160],
                    })
            entities.append({
                "region_id": rid,
                "label": str(item.get("label") or "").strip(),
                "kind": region_kind.get(rid, "other"),
                "decision": decision,
                "visual_identity": visual_identity,
                "canonical_name": canonical,
                "display_name": display_name,
                "entity_type": str(item.get("entity_type") or "unknown").strip(),
                "work": str(item.get("work") or "").strip(),
                "related_entities": rels,
                "confidence": confidence,
                "evidence": self._clean_list(item.get("evidence", []))[:3],
            })
        confs = [e["confidence"] for e in entities if e.get("confidence")]
        order = {"low": 0, "medium": 1, "high": 2}
        confidence = min(confs, key=lambda x: order[x]) if confs else self._normalize_confidence(data.get("confidence"))
        ocr_raw = data.get("ocr") if isinstance(data.get("ocr"), dict) else {}
        return {
            "status": "FINAL",
            "entities": entities,
            "overall_answer": str(data.get("overall_answer") or raw or "").strip(),
            "confidence": confidence,
            "ocr": {
                "text": str(ocr_raw.get("text") or primary.get("ocr") or "").strip(),
                "confidence": self._normalize_confidence(ocr_raw.get("confidence") or primary.get("confidence")),
            },
            "evidence": self._clean_list(data.get("evidence", []))[:6],
            "uncertainty": str(data.get("uncertainty") or "").strip(),
            "sources": self._clean_list(data.get("sources", []))[:4],
        }

    def _render_multi_typed_answer(self, entities: list[dict[str, Any]], ocr_text: str = "") -> str:
        parts: list[str] = []
        for item in entities:
            if str(item.get("kind") or "").strip() == "text":
                continue
            if str(item.get("entity_type") or "").strip() == "other" and str(item.get("label") or "").find("文字") >= 0:
                continue
            label = str(item.get("label") or item.get("region_id") or "对象").strip()
            name = str(item.get("display_name") or item.get("visual_identity") or "").strip()
            if not name:
                continue
            work = str(item.get("work") or "").strip()
            etype = str(item.get("entity_type") or "unknown").strip()
            type_labels = {
                "virtual_artist": "虚拟艺人",
                "music_isotope": "音乐同位体/衍生声库角色",
                "game_character": "游戏角色",
                "anime_character": "动画角色",
                "project_character": "企划角色",
                "product": "商品",
                "object": "物品",
            }
            suffix = []
            if etype in type_labels:
                suffix.append(type_labels[etype])
            if work:
                suffix.append(work)
            tail = f"（{'；'.join(suffix)}）" if suffix else ""
            parts.append(f"{label}：{name}{tail}")
        if not parts:
            return ""
        answer = "；".join(parts)
        if ocr_text:
            answer += f"。图中文字：{ocr_text}"
        return answer

    async def _animetrace_validate_rank1(
        self,
        *,
        event: AstrMessageEvent,
        search_provider: str,
        candidates: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], int, bool]:
        """Validate/localize AnimeTrace rank1 with at most one targeted web search.

        This method never edits, drops or reorders the AnimeTrace candidate list.
        """
        if not candidates:
            return {}, 0, False
        rank1 = candidates[0]
        canonical = str(rank1.get("canonical_name") or rank1.get("name") or "").strip()
        work = str(rank1.get("work") or "UNKNOWN").strip()
        if not canonical:
            return {}, 0, False

        key = f"{canonical.casefold()}::{work.casefold()}"
        ttl = max(300, int(self.config.get("animetrace_name_cache_ttl_seconds", 86400) or 86400))
        cached = self._animetrace_name_cache.get(key)
        if cached and time.monotonic() - float(cached.get("timestamp") or 0.0) <= ttl:
            data = dict(cached.get("result") or {})
            data["cache_hit"] = True
            return data, 0, False

        want_zh = bool(self.config.get("animetrace_resolve_chinese_names", True))
        zh_terms = " 中文名 中文译名" if want_zh else ""
        # AnimeTrace often returns a Japanese title plus a Latin title in the same work field.
        # Quoting the whole mixed string is too restrictive (e.g. ブルーアーカイブ -Blue Archive-).
        # Prefer a stable Latin fragment when available so Chinese/Japanese wiki pages can match.
        search_work = self._animetrace_search_work_term(work)
        if search_work:
            query = f'"{canonical}" "{search_work}" 角色 wiki 官方{zh_terms}'
        elif work and work != "UNKNOWN":
            query = f'"{canonical}" {work} 角色 wiki 官方{zh_terms}'
        else:
            query = f'"{canonical}" 角色 wiki 官方{zh_terms}'

        try:
            text = await self._native_tavily_search(event, query)
        except Exception as exc:  # noqa: BLE001
            return {}, 1, self._is_rate_limited(str(exc))
        if self._is_rate_limited(text):
            return {}, 1, True

        compact = self._compact_search_text(text)
        if not compact:
            return {}, 1, False

        try:
            validation_raw, _ = await asyncio.wait_for(
                self._llm_json(
                    search_provider,
                    ANIMETRACE_VALIDATE_SYSTEM,
                    json.dumps(
                        {
                            "immutable_candidates": [self._candidate_for_prompt(x) for x in candidates[:3]],
                            "rank1": self._candidate_for_prompt(rank1),
                            "web_search_result": compact,
                            "resolve_chinese_name": want_zh,
                        },
                        ensure_ascii=False,
                    ),
                ),
                timeout=self._search_worker_timeout(),
            )
            validation = self._normalize_animetrace_validation(validation_raw, rank1)
        except asyncio.TimeoutError:
            logger.warning("[vision-pipeline] AnimeTrace web validation LLM timed out; keeping rank1 without external conflict")
            validation = self._normalize_animetrace_validation({}, rank1)
            validation["missing_evidence"] = ["候选定向资料整理超时；未产生外部 HARD_CONFLICT。"]
        validation["cache_hit"] = False
        self._animetrace_name_cache[key] = {
            "timestamp": time.monotonic(),
            "result": dict(validation),
        }
        self._prune_name_cache()
        return validation, 1, False

    async def _verify_and_finish(
        self,
        *,
        event: AstrMessageEvent,
        task: str,
        requested_subtasks: list[str],
        rejected_candidates: list[str],
        images: list[str],
        primary: dict[str, Any],
        candidates: list[dict[str, Any]],
        web_validation: dict[str, Any],
        search_recommendation: str,
        candidate_source: str,
        vision_provider: str,
        reviewer_provider: str,
        allow_animetrace_rank1_guard: bool,
        counters: tuple[int, int, int, bool, int, int, bool, str],
    ) -> dict[str, Any]:
        (
            vision_calls, search_calls, reviewer_calls, search_blocked,
            animetrace_calls, animetrace_cache_hits, animetrace_blocked, grounding_path,
        ) = counters

        verify_prompt = json.dumps(
            {
                "task": task,
                "requested_subtasks": requested_subtasks,
                "rejected_candidates": rejected_candidates,
                "primary_observations": self._compact_primary(primary),
                "candidate_source": candidate_source,
                "grounded_candidates": [self._candidate_for_prompt(x) for x in candidates[:5]],
                "web_validation": web_validation,
                "recommendation": search_recommendation,
            },
            ensure_ascii=False,
        )
        verified, raw = await self._llm_json(
            vision_provider,
            VERIFY_VISION_SYSTEM,
            verify_prompt,
            image_urls=images,
        )
        vision_calls += 1
        verified = self._normalize_final(verified, raw, requested_subtasks)
        verified = self._enforce_grounded_selection(verified, candidates, rejected_candidates)
        self._debug("VISION_VERIFY", verified)

        rank1 = self._first_eligible_candidate(candidates, rejected_candidates)
        anime_rank1_external_ok = bool(
            allow_animetrace_rank1_guard
            and rank1
            and str(rank1.get("source_type") or "") == "animetrace"
            and not bool(rank1.get("not_confident", False))
            and not self._candidate_has_hard_conflict(rank1, web_validation)
        )

        # Reviewer is advisory only. It must never make the whole high-level tool fail/timeout.
        review2: dict[str, Any] = {}
        if self.config.get("enable_reviewer", True):
            try:
                review2, _ = await asyncio.wait_for(
                    self._llm_json(
                        reviewer_provider,
                        FINAL_REVIEW_SYSTEM,
                        json.dumps(
                            {
                                "task": task,
                                "requested_subtasks": requested_subtasks,
                                "rejected_candidates": rejected_candidates,
                                "primary": self._compact_primary(primary),
                                "provided_candidates": [self._candidate_for_prompt(x) for x in candidates[:5]],
                                "web_validation": web_validation,
                                "verified": verified,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                    timeout=self._reviewer_timeout(),
                )
                reviewer_calls += 1
                self._debug("FINAL_REVIEW", review2)
            except asyncio.TimeoutError:
                logger.warning("[vision-pipeline] final reviewer timed out; using completed Vision Verify result")
                self._debug("FINAL_REVIEW_SKIPPED", {"reason": "timeout"})
            except Exception as exc:  # noqa: BLE001
                logger.warning("[vision-pipeline] final reviewer failed (%s); using completed Vision Verify result", type(exc).__name__)
                self._debug("FINAL_REVIEW_SKIPPED", {"reason": type(exc).__name__})

        action = str(review2.get("action", "")).upper()
        if action == "ABSTAIN":
            # For an AnimeTrace rank1 with no user rejection and no external HARD_CONFLICT,
            # a text-only reviewer is not allowed to veto the specialized visual candidate.
            if not anime_rank1_external_ok:
                verified = self._abstain_identity_only(
                    verified,
                    reason=str(review2.get("reason") or "最终一致性审查发现关键矛盾。"),
                )
        elif action == "OVERCAUTIOUS" and anime_rank1_external_ok:
            if not self._clean_list(verified.get("selected_candidate_names", [])):
                verified = self._promote_rank1_medium(
                    verified,
                    rank1,
                    reason=str(review2.get("reason") or "最终视觉核验可能过度保守。"),
                )

        # Deterministic AnimeTrace guard: model-memory-only claims such as “I remember Kei looks
        # different” are NOT external hard conflicts. If rank1 survived user rejection and targeted
        # web validation found no HARD_CONFLICT, never discard it completely; keep it as a medium
        # confidence fallback. This is code-enforced and does not require a third Vision call.
        if (
            bool(self.config.get("animetrace_rank1_fallback_medium", True))
            and anime_rank1_external_ok
            and not self._clean_list(verified.get("selected_candidate_names", []))
        ):
            vision_only_conflicts = self._clean_list(verified.get("hard_conflicts", []))
            verified = self._promote_rank1_medium(
                verified,
                rank1,
                reason=(
                    "Final Vision 未选择专用识别器 rank1，但该候选未被用户明确否定，"
                    "且外部核验没有 HARD_CONFLICT；模型自身记忆型冲突不作为淘汰依据。"
                ),
            )
            if vision_only_conflicts:
                verified["uncertainty"] = (
                    str(verified.get("uncertainty") or "")
                    + " Vision 曾报告模型侧冲突：" + "；".join(vision_only_conflicts[:2])
                ).strip()
            # Do not leak model-only conflict as authoritative HARD_CONFLICT after promotion.
            verified["hard_conflicts"] = []

        result = self._final_from_verified(
            verified=verified,
            requested_subtasks=requested_subtasks,
            primary=primary,
            candidates=candidates,
        )
        if not result.get("sources"):
            result["sources"] = self._sources_from_candidates(candidates)
        result["_images"] = images
        result["meta"] = self._meta(
            vision_calls, search_calls, reviewer_calls, search_blocked,
            animetrace_calls, animetrace_cache_hits, animetrace_blocked, grounding_path,
        )
        return result

    def _normalize_animetrace_validation(
        self,
        data: dict[str, Any],
        rank1: dict[str, Any],
    ) -> dict[str, Any]:
        canonical = str(rank1.get("canonical_name") or rank1.get("name") or "").strip()
        work = str(rank1.get("work") or "UNKNOWN").strip()
        display_name = str(data.get("display_name") or "").strip() or canonical
        aliases_zh = self._clean_list(data.get("aliases_zh", []))[:5]
        if display_name != canonical and display_name not in aliases_zh:
            aliases_zh.insert(0, display_name)
        entity_exists = str(data.get("entity_exists") or "unknown").lower()
        work_match = str(data.get("work_match") or "unknown").lower()
        if entity_exists not in {"true", "false", "unknown"}:
            entity_exists = "unknown"
        if work_match not in {"true", "false", "unknown"}:
            work_match = "unknown"
        hard_conflicts = self._clean_list(data.get("hard_conflicts", []))[:4]
        if entity_exists == "false" and "网页资料明确否定该候选实体/名称真实性。" not in hard_conflicts:
            hard_conflicts.append("网页资料明确否定该候选实体/名称真实性。")
        if work_match == "false" and "网页资料明确显示候选所属作品与 AnimeTrace 输出不一致。" not in hard_conflicts:
            hard_conflicts.append("网页资料明确显示候选所属作品与 AnimeTrace 输出不一致。")
        hard_conflicts = hard_conflicts[:4]
        return {
            "canonical_name": canonical,
            "display_name": display_name,
            "aliases_zh": aliases_zh,
            "work": work,
            "display_work": str(data.get("display_work") or "").strip() or work,
            "entity_exists": entity_exists,
            "work_match": work_match,
            "support": self._clean_list(data.get("support", []))[:4],
            "hard_conflicts": hard_conflicts,
            "missing_evidence": self._clean_list(data.get("missing_evidence", []))[:4],
            "sources": self._clean_list(data.get("sources", []))[:4],
        }

    def _enrich_animetrace_candidates(
        self,
        candidates: list[dict[str, Any]],
        validation: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Add display aliases to a copy while preserving canonical order/rank."""
        canonical = str(validation.get("canonical_name") or "").strip()
        out: list[dict[str, Any]] = []
        for item in candidates:
            cand = dict(item)
            if str(cand.get("canonical_name") or cand.get("name") or "").strip() == canonical:
                cand["display_name"] = str(validation.get("display_name") or canonical)
                cand["aliases_zh"] = self._clean_list(validation.get("aliases_zh", []))[:5]
                cand["display_work"] = str(validation.get("display_work") or cand.get("work") or "UNKNOWN")
                cand["web_validation"] = dict(validation)
            out.append(cand)
        return out

    def _candidate_for_prompt(self, cand: dict[str, Any]) -> dict[str, Any]:
        return {
            "canonical_name": str(cand.get("canonical_name") or cand.get("name") or "").strip(),
            "display_name": str(cand.get("display_name") or cand.get("canonical_name") or cand.get("name") or "").strip(),
            "aliases_zh": self._clean_list(cand.get("aliases_zh", []))[:5],
            "work": str(cand.get("work") or "UNKNOWN").strip(),
            "display_work": str(cand.get("display_work") or cand.get("work") or "UNKNOWN").strip(),
            "rank": int(cand.get("rank") or 999),
            "box_index": int(cand.get("box_index") or 0),
            "not_confident": bool(cand.get("not_confident", False)),
            "source_type": str(cand.get("source_type") or "web"),
            "support": self._clean_list(cand.get("support", []))[:3],
            "hard_conflicts": self._clean_list(cand.get("hard_conflicts", cand.get("conflict", [])))[:3],
            "sources": self._clean_list(cand.get("sources", []))[:3],
        }

    def _first_eligible_candidate(
        self,
        candidates: list[dict[str, Any]],
        rejected_candidates: list[str],
    ) -> dict[str, Any] | None:
        for cand in candidates:
            name = str(cand.get("canonical_name") or cand.get("name") or "").strip()
            if name and not self._candidate_rejected(name, rejected_candidates):
                return cand
        return None

    def _candidate_has_hard_conflict(
        self,
        candidate: dict[str, Any],
        web_validation: dict[str, Any],
    ) -> bool:
        if self._clean_list(candidate.get("hard_conflicts", candidate.get("conflict", []))):
            return True
        canonical = str(candidate.get("canonical_name") or candidate.get("name") or "").strip()
        if canonical and canonical == str(web_validation.get("canonical_name") or "").strip():
            return bool(self._clean_list(web_validation.get("hard_conflicts", [])))
        return False

    def _abstain_identity_only(self, verified: dict[str, Any], reason: str) -> dict[str, Any]:
        out = dict(verified)
        out["selected_candidate_names"] = []
        out["identity"] = {
            "answer": "无法可靠确认具体身份。",
            "canonical_name": "",
            "display_name": "",
            "work": "",
            "confidence": "low",
        }
        out["uncertainty"] = reason
        return out

    def _promote_rank1_medium(
        self,
        verified: dict[str, Any],
        candidate: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        out = dict(verified)
        canonical = str(candidate.get("canonical_name") or candidate.get("name") or "").strip()
        display = str(candidate.get("display_name") or canonical).strip() or canonical
        work = str(candidate.get("display_work") or candidate.get("work") or "").strip()
        label = f"{display}（{canonical}）" if canonical and display and display != canonical else (display or canonical)
        answer = f"最可能是{label}"
        if work and work != "UNKNOWN":
            answer += f"，出自《{work}》"
        answer += "。"
        out["selected_candidate_names"] = [canonical] if canonical else []
        out["identity"] = {
            "answer": answer,
            "canonical_name": canonical,
            "display_name": display,
            "work": work,
            "confidence": "medium",
        }
        extra = "专用识别器 rank1 无硬冲突，最终视觉核验可能过度保守；按最可能候选保留中等置信度。"
        out["uncertainty"] = (str(out.get("uncertainty") or "") + " " + reason + " " + extra).strip()
        return out

    def _should_use_animetrace(self, primary: dict[str, Any], task: str) -> bool:
        if not self.config.get("animetrace_enabled", True):
            return False
        mode = str(self.config.get("animetrace_trigger_mode", "auto") or "auto")
        if mode == "all_entity_identity":
            return True
        if mode == "off":
            return False
        domain = str(primary.get("entity_domain") or "unknown")
        if domain == "anime_game_2d" or bool(primary.get("animetrace_recommended")):
            return True
        low = task.lower()
        hints = ["动漫", "动画", "二次元", "galgame", "游戏角色", "vtuber", "vup", "表情包角色"]
        return any(x in low for x in hints)

    async def _animetrace_recognize(
        self,
        image_ref: str,
        rejected_candidates: list[str],
    ) -> dict[str, Any]:
        if not self.config.get("animetrace_enabled", True):
            return {"status": "DISABLED", "candidates": [], "api_calls": 0, "blocked": False}

        now = time.monotonic()
        if now < self._animetrace_cooldown_until:
            return {
                "status": "BLOCKED",
                "candidates": [],
                "api_calls": 0,
                "blocked": True,
                "reason": self._animetrace_cooldown_reason or "AnimeTrace 处于冷却期",
            }

        try:
            image_bytes, filename, content_type = await self._read_image_bytes(image_ref)
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "ERROR",
                "candidates": [],
                "api_calls": 0,
                "blocked": False,
                "reason": f"读取图片失败：{type(exc).__name__}",
            }

        max_bytes = max(1, int(self.config.get("animetrace_max_file_mb", 10) or 10)) * 1024 * 1024
        if len(image_bytes) > max_bytes:
            return {
                "status": "SKIPPED",
                "candidates": [],
                "api_calls": 0,
                "blocked": False,
                "reason": f"图片超过 AnimeTrace 插件侧上限 {max_bytes // 1024 // 1024}MB",
            }

        digest = hashlib.sha256(image_bytes).hexdigest()
        cache_ttl = max(30, int(self.config.get("animetrace_cache_ttl_seconds", 1800) or 1800))
        cached = self._animetrace_cache.get(digest)
        if cached and now - float(cached.get("timestamp") or 0.0) <= cache_ttl:
            # Cache stores the raw AnimeTrace answer. Rejections are a per-request view only
            # and must never mutate/corrupt the shared image cache.
            state = copy.deepcopy(cached.get("result") or {})
            state["cache_hit"] = True
            state["api_calls"] = 0
            state["candidates"] = self._filter_rejected_candidates(
                list(state.get("candidates") or []), rejected_candidates
            )
            return state

        async with self._animetrace_lock:
            now = time.monotonic()
            if now < self._animetrace_cooldown_until:
                return {
                    "status": "BLOCKED",
                    "candidates": [],
                    "api_calls": 0,
                    "blocked": True,
                    "reason": self._animetrace_cooldown_reason or "AnimeTrace 处于冷却期",
                }

            min_interval = max(0.0, float(self.config.get("animetrace_min_interval_seconds", 2.0) or 0.0))
            wait = min_interval - (now - self._animetrace_last_call_at)
            if wait > 0:
                await asyncio.sleep(wait)

            model = await self._get_animetrace_model()
            session = await self._ensure_http_session()
            form = aiohttp.FormData()
            form.add_field("is_multi", "1")
            form.add_field("ai_detect", "0")
            if model:
                form.add_field("model", model)
            form.add_field("file", image_bytes, filename=filename, content_type=content_type)

            api_url = str(
                self.config.get("animetrace_api_url")
                or "https://api.animetrace.com/v1/search"
            ).strip()
            timeout = max(5, int(self.config.get("animetrace_timeout_seconds", 25) or 25))
            self._animetrace_last_call_at = time.monotonic()

            try:
                async with session.post(
                    api_url,
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as response:
                    retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
                    raw_text = await response.text()
                    try:
                        payload = json.loads(raw_text)
                    except Exception:
                        payload = {}

                    if response.status == 429:
                        self._set_animetrace_cooldown(
                            max(retry_after, self._animetrace_cooldown_seconds()),
                            "HTTP 429 rate limited",
                        )
                        return {
                            "status": "BLOCKED",
                            "candidates": [],
                            "api_calls": 1,
                            "blocked": True,
                            "reason": "AnimeTrace HTTP 429，已进入冷却并自动降级",
                        }

                    code = payload.get("code")
                    try:
                        code_int = int(code) if code is not None else response.status
                    except Exception:
                        code_int = response.status

                    if response.status >= 500 and code_int not in ANIMETRACE_SUCCESS_CODES:
                        self._set_animetrace_cooldown(
                            max(retry_after, self._animetrace_cooldown_seconds()),
                            f"HTTP {response.status} / code {code_int}",
                        )
                        return {
                            "status": "BLOCKED",
                            "candidates": [],
                            "api_calls": 1,
                            "blocked": True,
                            "reason": f"AnimeTrace 服务繁忙 HTTP {response.status}，已自动降级",
                        }

                    if code_int not in ANIMETRACE_SUCCESS_CODES:
                        reason = str(
                            payload.get("zh_message")
                            or payload.get("message")
                            or ANIMETRACE_ERRORS.get(code_int)
                            or f"AnimeTrace code={code_int}"
                        )
                        if code_int in ANIMETRACE_QUOTA_CODES:
                            self._set_animetrace_cooldown(
                                max(
                                    self._animetrace_cooldown_seconds(),
                                    int(self.config.get("animetrace_quota_cooldown_seconds", 600) or 600),
                                ),
                                reason,
                            )
                            return {
                                "status": "BLOCKED",
                                "candidates": [],
                                "api_calls": 1,
                                "blocked": True,
                                "reason": reason,
                            }
                        if code_int in ANIMETRACE_TEMPORARY_CODES:
                            self._set_animetrace_cooldown(
                                max(retry_after, self._animetrace_cooldown_seconds()),
                                reason,
                            )
                            return {
                                "status": "BLOCKED",
                                "candidates": [],
                                "api_calls": 1,
                                "blocked": True,
                                "reason": reason,
                            }
                        if code_int in ANIMETRACE_MAINTENANCE_CODES:
                            self._set_animetrace_cooldown(
                                max(300, self._animetrace_cooldown_seconds()),
                                reason,
                            )
                        return {
                            "status": "ERROR",
                            "candidates": [],
                            "api_calls": 1,
                            "blocked": code_int in ANIMETRACE_MAINTENANCE_CODES,
                            "reason": reason,
                        }

                    raw_result = self._parse_animetrace_payload(payload, model)
                    raw_result["api_calls"] = 1
                    raw_result["cache_hit"] = False
                    raw_result["blocked"] = False
                    # Persist only the unfiltered service response. User rejections are request-local.
                    self._animetrace_cache[digest] = {
                        "timestamp": time.monotonic(),
                        "result": copy.deepcopy(raw_result),
                    }
                    self._prune_animetrace_cache()
                    result = copy.deepcopy(raw_result)
                    result["candidates"] = self._filter_rejected_candidates(
                        result.get("candidates") or [], rejected_candidates
                    )
                    return result
            except asyncio.TimeoutError:
                self._set_animetrace_cooldown(
                    min(30, self._animetrace_cooldown_seconds()),
                    "AnimeTrace timeout",
                )
                return {
                    "status": "ERROR",
                    "candidates": [],
                    "api_calls": 1,
                    "blocked": False,
                    "reason": "AnimeTrace 请求超时，已自动降级",
                }
            except aiohttp.ClientError as exc:
                return {
                    "status": "ERROR",
                    "candidates": [],
                    "api_calls": 1,
                    "blocked": False,
                    "reason": f"AnimeTrace 网络错误：{type(exc).__name__}",
                }

    def _parse_animetrace_payload(self, payload: dict[str, Any], model: str) -> dict[str, Any]:
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            rows = []
        max_total = max(1, int(self.config.get("animetrace_max_candidates", 6) or 6))
        max_per_box = max(1, int(self.config.get("animetrace_max_candidates_per_box", 3) or 3))
        candidates: list[dict[str, Any]] = []
        any_not_confident = False

        for box_index, item in enumerate(rows, start=1):
            if not isinstance(item, dict):
                continue
            not_confident = bool(item.get("not_confident", False))
            any_not_confident = any_not_confident or not_confident
            chars = item.get("character")
            if not isinstance(chars, list):
                continue
            for rank, char in enumerate(chars[:max_per_box], start=1):
                if not isinstance(char, dict):
                    continue
                name = str(char.get("character") or "").strip()
                work = str(char.get("work") or "UNKNOWN").strip()
                if not name:
                    continue
                relevance = "high" if rank == 1 and not not_confident else ("medium" if rank <= 2 else "low")
                support = [
                    f"AnimeTrace 第{box_index}个人物框候选排名 {rank}",
                    f"AnimeTrace not_confident={str(not_confident).lower()}",
                ]
                candidate = {
                    "canonical_name": name,
                    "display_name": name,
                    "aliases_zh": [],
                    "name": name,  # compatibility alias; canonical_name is authoritative
                    "work": work,
                    "display_work": work,
                    "search_relevance": relevance,
                    "support": support,
                    "hard_conflicts": [],
                    "sources": [f"AnimeTrace ({model or 'service-default'})"],
                    "source_type": "animetrace",
                    "box_index": box_index,
                    "rank": rank,
                    "not_confident": not_confident,
                }
                key = (name.casefold(), work.casefold())
                if not any(
                    (str(x.get("canonical_name") or x.get("name", "")).casefold(), str(x.get("work", "")).casefold()) == key
                    for x in candidates
                ):
                    candidates.append(candidate)
                if len(candidates) >= max_total:
                    break
            if len(candidates) >= max_total:
                break

        return {
            "status": "MATCH" if candidates else "NO_MATCH",
            "candidates": candidates,
            "not_confident": any_not_confident,
            "trace_id": str(payload.get("trace_id") or ""),
            "model": model,
            "ai_detected": bool(payload.get("ai", False)),
            "reason": "" if candidates else "AnimeTrace 未返回具名候选",
        }

    async def _get_animetrace_model(self) -> str:
        configured = str(self.config.get("animetrace_model_id") or "").strip()
        if configured:
            return configured

        ttl = max(300, int(self.config.get("animetrace_model_cache_ttl_seconds", 3600) or 3600))
        if self._animetrace_models and time.monotonic() - self._animetrace_model_cache_at <= ttl:
            return self._select_default_animetrace_model(self._animetrace_models)

        session = await self._ensure_http_session()
        url = str(
            self.config.get("animetrace_model_list_url")
            or "https://api.animetrace.com/v1/model/list"
        ).strip()
        timeout = max(5, int(self.config.get("animetrace_timeout_seconds", 25) or 25))
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                if response.status != 200:
                    return ""
                payload = await response.json(content_type=None)
                rows = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(rows, list):
                    self._animetrace_models = [x for x in rows if isinstance(x, dict)]
                    self._animetrace_model_cache_at = time.monotonic()
                    return self._select_default_animetrace_model(self._animetrace_models)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[vision-pipeline] AnimeTrace model list failed: %s", exc)
        # model is optional per AnimeTrace API; omit it if list is temporarily unavailable.
        return ""

    @staticmethod
    def _select_default_animetrace_model(models: list[dict[str, Any]]) -> str:
        enabled = [m for m in models if m.get("enabled", False)]
        if not enabled:
            return ""
        chosen = next((m for m in enabled if m.get("default", False)), enabled[0])
        return str(chosen.get("id") or "").strip()

    async def _read_image_bytes(self, ref: str) -> tuple[bytes, str, str]:
        if os.path.isfile(ref):
            path = Path(ref)
            data = path.read_bytes()
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return data, path.name or "image.jpg", ctype

        if ref.startswith("file://"):
            path_text = urllib.parse.unquote(ref[7:])
            if os.name == "nt" and path_text.startswith("/"):
                path_text = path_text[1:]
            path = Path(path_text)
            data = path.read_bytes()
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return data, path.name or "image.jpg", ctype

        if ref.startswith(("http://", "https://")):
            session = await self._ensure_http_session()
            timeout = max(5, int(self.config.get("animetrace_timeout_seconds", 25) or 25))
            async with session.get(ref, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                if response.status != 200:
                    raise RuntimeError(f"image download HTTP {response.status}")
                data = await response.read()
                name = Path(urllib.parse.urlparse(ref).path).name or "image.jpg"
                ctype = response.headers.get("Content-Type") or mimetypes.guess_type(name)[0] or "application/octet-stream"
                return data, name, ctype.split(";", 1)[0]

        raise RuntimeError("unsupported image reference")

    async def _ensure_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(trust_env=True)
        return self._http_session

    def _set_animetrace_cooldown(self, seconds: int, reason: str) -> None:
        seconds = max(1, int(seconds))
        self._animetrace_cooldown_until = max(
            self._animetrace_cooldown_until,
            time.monotonic() + seconds,
        )
        self._animetrace_cooldown_reason = reason
        logger.warning("[vision-pipeline] AnimeTrace cooldown %ss: %s", seconds, reason)

    def _animetrace_cooldown_seconds(self) -> int:
        return max(5, int(self.config.get("animetrace_cooldown_seconds", 90) or 90))

    @staticmethod
    def _parse_retry_after(value: str | None) -> int:
        try:
            return max(0, int(float(value or 0)))
        except Exception:
            return 0

    def _prune_animetrace_cache(self) -> None:
        ttl = max(30, int(self.config.get("animetrace_cache_ttl_seconds", 1800) or 1800))
        now = time.monotonic()
        stale = [k for k, v in self._animetrace_cache.items() if now - float(v.get("timestamp") or 0) > ttl]
        for key in stale:
            self._animetrace_cache.pop(key, None)
        # Hard cap prevents unbounded memory growth in busy groups.
        while len(self._animetrace_cache) > 128:
            oldest = min(self._animetrace_cache, key=lambda k: float(self._animetrace_cache[k].get("timestamp") or 0))
            self._animetrace_cache.pop(oldest, None)

    def _prune_name_cache(self) -> None:
        ttl = max(300, int(self.config.get("animetrace_name_cache_ttl_seconds", 86400) or 86400))
        now = time.monotonic()
        stale = [
            k for k, v in self._animetrace_name_cache.items()
            if now - float(v.get("timestamp") or 0.0) > ttl
        ]
        for key in stale:
            self._animetrace_name_cache.pop(key, None)
        while len(self._animetrace_name_cache) > 256:
            oldest = min(
                self._animetrace_name_cache,
                key=lambda k: float(self._animetrace_name_cache[k].get("timestamp") or 0.0),
            )
            self._animetrace_name_cache.pop(oldest, None)

    def _sanitize_user_rejections(
        self,
        *,
        event: AstrMessageEvent,
        cache_key: str,
        proposed: list[str],
    ) -> list[str]:
        """Accept only rejections with user-side provenance.

        The Main LLM may accidentally include candidates that were merely discussed or
        model-rejected. A candidate becomes an actual rejection only when the current user
        message names it, or when the current message is an explicit correction of the last
        LOCKED identity.
        """
        proposed = self._clean_list(proposed)
        if not proposed:
            return []
        user_text = str(event.message_str or "")
        user_norm = re.sub(r"\s+", "", user_text).casefold()
        cached = self._last_by_user.get(cache_key) or {}
        cached_names = self._clean_list([
            cached.get("identity_canonical", ""),
            cached.get("identity_exact", ""),
        ])
        correction = self._looks_like_correction(user_text)
        accepted: list[str] = []
        ignored: list[str] = []
        for item in proposed:
            norm = re.sub(r"\s+", "", item).casefold()
            literal = bool(norm and norm in user_norm)
            previous_locked = bool(
                correction
                and any(self._candidate_rejected(old, [item]) for old in cached_names)
            )
            if literal or previous_locked:
                accepted.append(item)
            else:
                ignored.append(item)
        if ignored:
            self._debug(
                "REJECT_PROVENANCE_GUARD",
                {"accepted": accepted, "ignored_model_supplied": ignored, "user_text": user_text},
            )
        return accepted

    @staticmethod
    def _animetrace_search_work_term(work: str) -> str:
        work = str(work or "").strip()
        if not work or work == "UNKNOWN":
            return ""
        # Prefer a Latin/ASCII title embedded in mixed Japanese + Latin work strings.
        parts = re.findall(r"[A-Za-z][A-Za-z0-9&+:' ._-]{2,}", work)
        cleaned = []
        for part in parts:
            item = part.strip(" -_.,")
            if len(item) >= 3 and item not in cleaned:
                cleaned.append(item)
        if cleaned:
            return max(cleaned, key=len)
        return work

    def _reviewer_timeout(self) -> float:
        try:
            return max(3.0, min(float(self.config.get("reviewer_timeout_seconds", 12) or 12), 30.0))
        except Exception:
            return 12.0

    def _search_worker_timeout(self) -> float:
        try:
            return max(10.0, min(float(self.config.get("search_worker_timeout_seconds", 30) or 30), 60.0))
        except Exception:
            return 30.0

    def _filter_rejected_candidates(
        self,
        candidates: list[dict[str, Any]],
        rejected_candidates: list[str],
    ) -> list[dict[str, Any]]:
        if not rejected_candidates:
            return candidates
        out = []
        for cand in candidates:
            name = str(cand.get("canonical_name") or cand.get("name") or "").strip()
            if name and self._candidate_rejected(name, rejected_candidates):
                continue
            out.append(cand)
        return out

    @staticmethod
    def _candidate_rejected(name: str, rejected_candidates: list[str]) -> bool:
        n = re.sub(r"\s+", "", name).casefold()
        if not n:
            return False
        for item in rejected_candidates:
            r = re.sub(r"\s+", "", str(item)).casefold()
            if n in r or (r and r in n):
                return True
        return False

    # -------------------- provider / tool calls --------------------

    async def _provider_id(self, event: AstrMessageEvent, key: str) -> str:
        configured = str(self.config.get(key) or "").strip()
        if configured:
            return configured
        return await self.context.get_current_chat_provider_id(event.unified_msg_origin)

    async def _llm_json(
        self,
        provider_id: str,
        system_prompt: str,
        prompt: str,
        image_urls: list[str] | None = None,
    ) -> tuple[dict[str, Any], str]:
        resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            system_prompt=system_prompt,
            prompt=prompt,
            image_urls=image_urls or [],
        )
        text = (resp.completion_text or "").strip()
        return self._parse_json(text), text

    def _run_context(self, event: AstrMessageEvent) -> AgentContextWrapper:
        return AgentContextWrapper(
            context=AstrAgentContext(context=self.context, event=event),
            tool_call_timeout=int(self.config.get("tool_timeout", 45) or 45),
        )

    async def _native_tavily_search(self, event: AstrMessageEvent, query: str) -> str:
        cfg = self.context.get_config(umo=event.unified_msg_origin)
        ps = cfg.get("provider_settings", {})
        if not ps.get("web_search", False) or ps.get("websearch_provider") != "tavily":
            raise RuntimeError("AstrBot 原生网页搜索未启用或当前 provider 不是 tavily。")
        if not ps.get("websearch_tavily_key", []):
            raise RuntimeError("AstrBot 未配置 Tavily API Key。")

        tool = self.context.get_llm_tool_manager().get_builtin_tool("web_search_tavily")
        kwargs = {
            "query": query,
            "max_results": int(self.config.get("tavily_max_results", 5) or 5),
            "search_depth": str(self.config.get("tavily_search_depth", "basic") or "basic"),
            "topic": "general",
        }
        timeout = int(self.config.get("tool_timeout", 45) or 45)
        result = await asyncio.wait_for(tool.call(self._run_context(event), **kwargs), timeout=timeout)
        return self._tool_result_text(result)

    async def _native_tavily_extract(self, event: AstrMessageEvent, url: str) -> str:
        tool = self.context.get_llm_tool_manager().get_builtin_tool("tavily_extract_web_page")
        timeout = int(self.config.get("tool_timeout", 45) or 45)
        result = await asyncio.wait_for(
            tool.call(self._run_context(event), url=url, extract_depth="basic"),
            timeout=timeout,
        )
        return self._tool_result_text(result)

    # -------------------- image collection / follow-up cache --------------------

    async def _collect_images(self, event: AstrMessageEvent) -> list[str]:
        refs: list[str] = []

        async def add_image(comp: Image) -> None:
            try:
                path = await comp.convert_to_file_path()
                if path and path not in refs:
                    refs.append(path)
                    return
            except Exception as exc:  # noqa: BLE001
                logger.debug("[vision-pipeline] convert_to_file_path failed: %s", exc)
            fallback = str(getattr(comp, "url", "") or getattr(comp, "file", "") or "").strip()
            if fallback and fallback not in refs:
                refs.append(fallback)

        for comp in event.get_messages():
            if isinstance(comp, Image):
                await add_image(comp)
            elif isinstance(comp, Reply):
                for sub in (getattr(comp, "chain", None) or []):
                    if isinstance(sub, Image):
                        await add_image(sub)
        return refs

    def _user_cache_key(self, event: AstrMessageEvent) -> str:
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:
            sender = ""
        return f"{event.unified_msg_origin}::{sender}"

    # -------------------- prompt / normalization --------------------

    def _primary_user_prompt(
        self,
        task: str,
        rejected: list[str],
        requested_subtasks: list[str],
    ) -> str:
        return json.dumps(
            {
                "task": task,
                "requested_subtasks": requested_subtasks,
                "rejected_candidates": rejected,
                "instruction": (
                    "独立查看原图；不要采用调用者对图片外观的猜测。"
                    "具体实体身份的内部知识只能作为 possible_leads，不得伪装成已验证的外部事实。"
                    "requested_subtasks 是固定任务契约，必须全部覆盖。"
                ),
            },
            ensure_ascii=False,
        )

    def _normalize_primary(self, data: dict[str, Any], raw: str) -> dict[str, Any]:
        status = str(data.get("status") or "").upper()
        if status not in {"FINAL", "NEED_SEARCH"}:
            status = "NEED_SEARCH" if "NEED_SEARCH" in raw.upper() else "FINAL"
        return {
            "status": status,
            "task_type": str(data.get("task_type") or "other"),
            "entity_domain": str(data.get("entity_domain") or "unknown"),
            "animetrace_recommended": bool(data.get("animetrace_recommended", False)),
            "final_answer": str(data.get("final_answer") or (raw if status == "FINAL" else "")).strip(),
            "confidence": self._normalize_confidence(data.get("confidence")),
            "certain_observations": self._clean_list(data.get("certain_observations", []))[:5],
            "uncertain_interpretations": self._clean_list(data.get("uncertain_interpretations", []))[:3],
            "ocr": str(data.get("ocr") or "").strip(),
            "meme_answer": str(data.get("meme_answer") or "").strip(),
            "search_request": str(data.get("search_request") or "").strip(),
            "query_hints": self._clean_list(data.get("query_hints", []))[:3],
            "possible_leads": self._clean_list(data.get("possible_leads", []))[:3],
            "external_claims": self._clean_list(data.get("external_claims", []))[:4],
            "evidence": self._clean_list(data.get("evidence", []))[:4],
            "uncertainty": str(data.get("uncertainty") or "").strip(),
            "strong_text_anchors": self._clean_list(data.get("strong_text_anchors", []))[:4],
            "regions": self._normalize_regions(data.get("regions", [])),
            "multi_object": bool(data.get("multi_object", False)),
        }

    def _normalize_search_state(self, data: dict[str, Any]) -> dict[str, Any]:
        candidates = data.get("candidates")
        if not isinstance(candidates, list):
            candidates = []
        clean_candidates = []
        for rank, item in enumerate(candidates[:3], start=1):
            if not isinstance(item, dict):
                continue
            canonical = str(item.get("canonical_name") or item.get("name") or "").strip()
            if not canonical:
                continue
            display = str(item.get("display_name") or canonical).strip() or canonical
            work = str(item.get("work") or "UNKNOWN").strip()
            clean_candidates.append(
                {
                    "canonical_name": canonical,
                    "display_name": display,
                    "aliases_zh": self._clean_list(item.get("aliases_zh", []))[:5],
                    "name": canonical,
                    "work": work,
                    "display_work": str(item.get("display_work") or work).strip() or work,
                    "rank": rank,
                    "box_index": 0,
                    "not_confident": False,
                    "search_relevance": str(item.get("search_relevance") or "low").strip(),
                    "support": self._clean_list(item.get("support", []))[:3],
                    "hard_conflicts": self._clean_list(item.get("hard_conflicts", item.get("conflict", [])))[:3],
                    "sources": self._clean_list(item.get("sources", []))[:2],
                    "source_type": "web",
                }
            )
        return {
            "status": "SEARCH_COMPLETE",
            "candidates": clean_candidates,
            "recommendation": str(data.get("recommendation") or "").strip(),
            "extract_url": str(data.get("extract_url") or "").strip(),
        }

    def _normalize_final(
        self,
        data: dict[str, Any],
        raw: str,
        requested_subtasks: list[str],
    ) -> dict[str, Any]:
        identity_raw = data.get("identity") if isinstance(data.get("identity"), dict) else {}
        ocr_raw = data.get("ocr") if isinstance(data.get("ocr"), dict) else {}
        meme_raw = data.get("meme") if isinstance(data.get("meme"), dict) else {}

        selected = self._clean_list(data.get("selected_candidate_names", []))[:5]
        identity = {
            "answer": str(identity_raw.get("answer") or "").strip(),
            "canonical_name": str(identity_raw.get("canonical_name") or "").strip(),
            "display_name": str(identity_raw.get("display_name") or "").strip(),
            "work": str(identity_raw.get("work") or "").strip(),
            "confidence": self._normalize_confidence(identity_raw.get("confidence")),
        }
        if "identity" in requested_subtasks and not identity["answer"]:
            identity["answer"] = "无法可靠确认具体身份。" if not selected else ""

        ocr = {
            "text": str(ocr_raw.get("text") or "").strip(),
            "confidence": self._normalize_confidence(ocr_raw.get("confidence")),
        }
        meme = {
            "answer": str(meme_raw.get("answer") or "").strip(),
            "confidence": self._normalize_confidence(meme_raw.get("confidence")),
        }

        # Compatibility fallback if a provider ignored the new nested schema.
        final_answer = str(data.get("final_answer") or "").strip()
        if "identity" in requested_subtasks and not identity["answer"] and final_answer:
            identity["answer"] = final_answer
        if "ocr" in requested_subtasks and not ocr["text"]:
            ocr["text"] = str(data.get("ocr_text") or "").strip()
        if "meme" in requested_subtasks and not meme["answer"]:
            meme["answer"] = str(data.get("meme_answer") or "").strip()

        return {
            "status": "FINAL",
            "selected_candidate_names": selected,
            "identity": identity,
            "ocr": ocr,
            "meme": meme,
            "evidence": self._clean_list(data.get("evidence", []))[:4],
            "hard_conflicts": self._clean_list(data.get("hard_conflicts", []))[:4],
            "uncertainty": str(data.get("uncertainty") or "").strip(),
            "sources": self._clean_list(data.get("sources", []))[:4],
            "raw_final_answer": final_answer or raw,
        }

    def _enforce_grounded_selection(
        self,
        verified: dict[str, Any],
        candidates: list[dict[str, Any]],
        rejected_candidates: list[str],
    ) -> dict[str, Any]:
        if not isinstance(verified.get("identity"), dict):
            verified["identity"] = {
                "answer": "无法可靠确认具体身份。",
                "canonical_name": "",
                "display_name": "",
                "work": "",
                "confidence": "low",
            }

        alias_map: dict[str, str] = {}
        by_canonical: dict[str, dict[str, Any]] = {}
        for cand in candidates:
            canonical = str(cand.get("canonical_name") or cand.get("name") or "").strip()
            if not canonical:
                continue
            by_canonical[canonical] = cand
            variants = [
                canonical,
                str(cand.get("display_name") or "").strip(),
                *self._clean_list(cand.get("aliases_zh", [])),
            ]
            for v in variants:
                if v:
                    alias_map[re.sub(r"\s+", "", v).casefold()] = canonical

        selected: list[str] = []
        for raw_name in self._clean_list(verified.get("selected_candidate_names", [])):
            key = re.sub(r"\s+", "", raw_name).casefold()
            canonical = alias_map.get(key)
            if canonical and canonical not in selected and not self._candidate_rejected(canonical, rejected_candidates):
                selected.append(canonical)

        identity = dict(verified.get("identity") or {})
        # Try identity names if model forgot selected_candidate_names.
        if not selected:
            for raw_name in [identity.get("canonical_name"), identity.get("display_name")]:
                key = re.sub(r"\s+", "", str(raw_name or "")).casefold()
                canonical = alias_map.get(key)
                if canonical and not self._candidate_rejected(canonical, rejected_candidates):
                    selected = [canonical]
                    break

        if selected:
            canonical = selected[0]
            cand = by_canonical[canonical]
            display = str(cand.get("display_name") or canonical).strip() or canonical
            work = str(cand.get("display_work") or cand.get("work") or "").strip()
            label = f"{display}（{canonical}）" if display != canonical else canonical
            identity["canonical_name"] = canonical
            identity["display_name"] = display
            identity["work"] = work
            identity["confidence"] = self._normalize_confidence(identity.get("confidence"))
            prefix = "最可能是" if identity["confidence"] == "medium" else ""
            identity["answer"] = prefix + f"{label}" + (f"，出自《{work}》" if work and work != "UNKNOWN" else "") + "。"
            verified["selected_candidate_names"] = [canonical]
            verified["identity"] = identity
            return verified

        verified["selected_candidate_names"] = []
        identity.update(
            {
                "answer": "无法可靠确认具体身份。",
                "canonical_name": "",
                "display_name": "",
                "work": "",
                "confidence": "low",
            }
        )
        verified["identity"] = identity
        return verified

    def _final_from_primary(
        self,
        primary: dict[str, Any],
        requested_subtasks: list[str],
    ) -> dict[str, Any]:
        final_answer = str(primary.get("final_answer") or "无法可靠确认。").strip()
        locked: dict[str, str] = {}
        sub_conf: dict[str, str] = {}
        if "ocr" in requested_subtasks and primary.get("ocr"):
            locked["OCR_EXACT"] = str(primary.get("ocr") or "")
            sub_conf["ocr"] = primary.get("confidence", "low")
        if "meme" in requested_subtasks:
            sub_conf["meme"] = primary.get("confidence", "low")
        return {
            "status": "FINAL",
            "final_answer": final_answer,
            "confidence": self._overall_confidence(sub_conf, primary.get("confidence", "low")),
            "subtask_confidence": sub_conf,
            "locked_facts": locked,
            "evidence": self._clean_list(primary.get("evidence", []))[:4],
            "uncertainty": str(primary.get("uncertainty") or "").strip(),
            "sources": self._clean_list(primary.get("sources", []))[:4],
        }

    def _final_from_verified(
        self,
        *,
        verified: dict[str, Any],
        requested_subtasks: list[str],
        primary: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        parts: list[str] = []
        locked: dict[str, str] = {}
        sub_conf: dict[str, str] = {}

        identity = verified.get("identity") if isinstance(verified.get("identity"), dict) else {}
        ocr = verified.get("ocr") if isinstance(verified.get("ocr"), dict) else {}
        meme = verified.get("meme") if isinstance(verified.get("meme"), dict) else {}

        if "identity" in requested_subtasks:
            answer = str(identity.get("answer") or "无法可靠确认具体身份。").strip()
            parts.append("角色身份：" + answer)
            sub_conf["identity"] = self._normalize_confidence(identity.get("confidence"))
            canonical = str(identity.get("canonical_name") or "").strip()
            display = str(identity.get("display_name") or "").strip()
            work = str(identity.get("work") or "").strip()
            if canonical:
                locked["IDENTITY_STATUS"] = "CONFIRMED"
                locked["IDENTITY_CANONICAL"] = canonical
                locked["IDENTITY_EXACT"] = display or canonical
            else:
                locked["IDENTITY_STATUS"] = "UNCONFIRMED"
            if work and work != "UNKNOWN":
                locked["WORK_EXACT"] = work

        if "ocr" in requested_subtasks:
            text = str(ocr.get("text") or primary.get("ocr") or "").strip()
            if text:
                parts.append(f"图中文字：{text}")
                locked["OCR_EXACT"] = text
            sub_conf["ocr"] = self._normalize_confidence(ocr.get("confidence") or primary.get("confidence"))

        if "meme" in requested_subtasks:
            answer = str(meme.get("answer") or primary.get("meme_answer") or "").strip()
            if answer:
                parts.append(answer)
            sub_conf["meme"] = self._normalize_confidence(meme.get("confidence") or primary.get("confidence"))

        if not parts:
            raw = str(verified.get("raw_final_answer") or "").strip()
            parts.append(raw or "无法可靠确认。")

        final_answer = "\n".join(parts)
        identity_unconfirmed = (
            "identity" in requested_subtasks
            and str(locked.get("IDENTITY_STATUS") or "") == "UNCONFIRMED"
        )
        # If identity is unconfirmed, do not hand Main strong candidate-specific claims from a
        # discarded Vision branch. Only expose direct Primary visual observations.
        evidence = (
            self._clean_list(primary.get("certain_observations", []))[:4]
            if identity_unconfirmed
            else self._clean_list(verified.get("evidence", []))[:4]
        )
        return {
            "status": "FINAL",
            "final_answer": final_answer,
            "confidence": self._overall_confidence(sub_conf, "low"),
            "subtask_confidence": sub_conf,
            "locked_facts": locked,
            "evidence": evidence,
            "uncertainty": str(verified.get("uncertainty") or "").strip(),
            "sources": self._clean_list(verified.get("sources", []))[:4] or self._sources_from_candidates(candidates),
        }

    def _compact_primary(self, primary: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": primary.get("status"),
            "task_type": primary.get("task_type"),
            "entity_domain": primary.get("entity_domain"),
            "certain_observations": primary.get("certain_observations", []),
            "uncertain_interpretations": primary.get("uncertain_interpretations", []),
            "ocr": primary.get("ocr", ""),
            "meme_answer": primary.get("meme_answer", ""),
            "search_request": primary.get("search_request", ""),
            "possible_leads": primary.get("possible_leads", []),
            "external_claims": primary.get("external_claims", []),
            "strong_text_anchors": primary.get("strong_text_anchors", []),
            "regions": primary.get("regions", []),
            "multi_object": primary.get("multi_object", False),
        }

    def _task_requests_identity(self, task: str) -> bool:
        t = task.lower()
        patterns = [
            "这是谁", "是谁", "哪个角色", "什么角色", "人物是谁", "角色是谁", "具体角色",
            "身份", "哪位", "叫什么", "角色名", "识别图中的角色", "识别角色", "图中的角色",
            "who is", "which character", "identify character", "identify person", "identify entity",
            "有哪些人物", "哪些人物", "分别是谁", "两位人物", "多个人物",
        ]
        return any(p in t for p in patterns)

    def _requested_subtasks(self, task: str) -> list[str]:
        t = task.lower()
        out: list[str] = []
        if self._task_requests_identity(task):
            out.append("identity")
        if any(p in t for p in ["ocr", "文字", "写了什么", "写的什么", "读出", "字幕", "图中文字", "字是什么"]):
            out.append("ocr")
        meme_intent = any(p in t for p in ["什么意思", "含义", "笑点", "表达的意思", "梗来源", "解读"])
        meme_intent = meme_intent or (
            any(k in t for k in ["表情包", "梗图"])
            and any(v in t for v in ["解释", "理解", "意思", "表达", "笑点", "解读"])
        )
        if meme_intent:
            out.append("meme")
        if any(p in t for p in ["ui", "界面", "截图", "按钮", "数值", "状态", "报错"]):
            out.append("ui")
        if any(p in t for p in ["比较", "区别", "差异", "compare"]):
            out.append("comparison")
        if not out:
            out.append("general")
        return out

    @staticmethod
    def _normalize_confidence(value: Any) -> str:
        v = str(value or "low").lower()
        return v if v in {"high", "medium", "low"} else "low"

    def _overall_confidence(self, sub: dict[str, str], fallback: Any = "low") -> str:
        if not sub:
            return self._normalize_confidence(fallback)
        order = {"low": 0, "medium": 1, "high": 2}
        vals = [self._normalize_confidence(v) for v in sub.values()]
        # Overall confidence reflects the weakest requested subtask, avoiding
        # "meme high" masking an unresolved identity.
        return min(vals, key=lambda x: order[x])

    def _initial_search_queries(
        self,
        primary: dict[str, Any],
        seed_candidates: list[dict[str, Any]],
        rejected_candidates: list[str],
    ) -> list[str]:
        queries: list[str] = []
        ocr = str(primary.get("ocr") or "").strip()
        if 1 <= len(ocr) <= 50:
            queries.append(f'"{ocr}"')

        for hint in self._clean_list(primary.get("query_hints", [])):
            if len(queries) >= self._max_search_queries():
                break
            if hint and hint not in queries:
                queries.append(hint)

        # Specialized candidate verification only after OCR/neutral hints.
        for cand in seed_candidates[:2]:
            if len(queries) >= self._max_search_queries():
                break
            name = str(cand.get("canonical_name") or cand.get("name") or "").strip()
            work = str(cand.get("work") or "").strip()
            if not name or self._candidate_rejected(name, rejected_candidates):
                continue
            q = f'"{name}" "{work}" 角色 官方' if work and work != "UNKNOWN" else f'"{name}" 角色 官方'
            if q not in queries:
                queries.append(q)

        # If Primary has a lead and no specialized recognizer did, it can be the last query only.
        if not seed_candidates:
            for lead in self._clean_list(primary.get("possible_leads", [])):
                if len(queries) >= self._max_search_queries():
                    break
                if self._candidate_rejected(lead, rejected_candidates):
                    continue
                q = f'"{lead}" 角色 作品 官方'
                if q not in queries:
                    queries.append(q)

        return queries[: self._max_search_queries()]

    # -------------------- Tavily compaction / parsing --------------------

    def _compact_search_text(self, text: str, *, max_items: int | None = None, snippet_chars: int | None = None) -> str:
        if max_items is None:
            max_items = max(1, int(self.config.get("max_search_items", 5) or 5))
        else:
            max_items = max(1, min(10, int(max_items)))
        if snippet_chars is None:
            snippet_chars = max(100, int(self.config.get("max_snippet_chars", 700) or 700))
        else:
            snippet_chars = max(100, min(2000, int(snippet_chars)))
        try:
            data = json.loads(text)
            rows = data.get("results", []) if isinstance(data, dict) else []
            compact = []
            for row in rows[:max_items]:
                if not isinstance(row, dict):
                    continue
                compact.append(
                    {
                        "title": str(row.get("title") or "")[:300],
                        "url": str(row.get("url") or "")[:1000],
                        "snippet": str(row.get("snippet") or row.get("content") or "")[:snippet_chars],
                    }
                )
            if compact:
                return json.dumps({"results": compact}, ensure_ascii=False)
        except Exception:
            pass
        return self._truncate(text, max_items * (snippet_chars + 500))

    @staticmethod
    def _tool_result_text(result: Any) -> str:
        if isinstance(result, str):
            return result
        content = getattr(result, "content", None)
        if isinstance(content, list):
            chunks = []
            for item in content:
                text = getattr(item, "text", None)
                if text:
                    chunks.append(str(text))
            if chunks:
                return "\n".join(chunks)
        return str(result)

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        if not text:
            return {}
        cleaned = text.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            obj = json.loads(cleaned)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            pass
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                obj = json.loads(cleaned[start : end + 1])
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {}
        return {}

    # -------------------- misc helpers --------------------

    def _max_search_queries(self) -> int:
        try:
            return max(1, min(3, int(self.config.get("max_search_queries", 3) or 3)))
        except Exception:
            return 3

    @staticmethod
    def _clean_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        out = []
        for item in value:
            text = str(item).strip()
            if text and text not in out:
                out.append(text)
        return out

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + "...<truncated>"

    @staticmethod
    def _looks_like_correction(task: str) -> bool:
        t = task.lower()
        patterns = [
            "不是她", "不是他", "不是这个", "认错", "识别错", "答案不对", "重新识别", "重新找",
            "wrong", "not her", "not him",
        ]
        return any(p in t for p in patterns)

    @staticmethod
    def _is_unknown_answer(text: str) -> bool:
        low = text.lower()
        return any(x in low for x in ["无法可靠确认", "无法确认", "不能确认", "不确定", "unknown", "cannot confirm"])


    @staticmethod
    def _is_rate_limited(text: str) -> bool:
        low = text.lower()
        tokens = [
            "429", "rate limit", "too many requests", "excessive requests",
            "quota exceeded", "status: 432",
        ]
        return any(x in low for x in tokens)

    def _sources_from_candidates(self, candidates: list[dict[str, Any]]) -> list[str]:
        sources: list[str] = []
        for cand in candidates:
            for src in cand.get("sources", []) if isinstance(cand, dict) else []:
                s = str(src).strip()
                if s and s not in sources:
                    sources.append(s)
        return sources[:4]

    def _meta(
        self,
        vision_calls: int,
        search_calls: int,
        reviewer_calls: int,
        search_blocked: bool,
        animetrace_calls: int,
        animetrace_cache_hits: int,
        animetrace_blocked: bool,
        grounding_path: str,
    ) -> dict[str, Any]:
        return {
            "vision_calls": vision_calls,
            "search_calls": search_calls,
            "reviewer_calls": reviewer_calls,
            "search_blocked": search_blocked,
            "animetrace_calls": animetrace_calls,
            "animetrace_cache_hits": animetrace_cache_hits,
            "animetrace_blocked": animetrace_blocked,
            "grounding_path": grounding_path,
        }

    def _format_result(self, result: dict[str, Any]) -> str:
        lines = [
            "VISION_PIPELINE_RESULT",
            f"STATUS: {result.get('status', 'FINAL')}",
            f"FINAL_ANSWER: {result.get('final_answer', '')}",
            f"CONFIDENCE: {result.get('confidence', 'low')}",
        ]

        sub_conf = result.get("subtask_confidence")
        if isinstance(sub_conf, dict) and sub_conf:
            lines.append("SUBTASK_CONFIDENCE: " + json.dumps(sub_conf, ensure_ascii=False))

        locked = result.get("locked_facts")
        if isinstance(locked, dict) and locked:
            lines.append("LOCKED_FACTS:")
            for key in ["IMAGE_STATUS", "IDENTITY_STATUS", "IDENTITY_EXACT", "IDENTITY_CANONICAL", "WORK_EXACT", "OCR_EXACT"]:
                value = str(locked.get(key) or "").strip()
                if value:
                    lines.append(f"{key}: {value}")

        evidence = self._clean_list(result.get("evidence", []))
        if evidence:
            lines.append("EVIDENCE:")
            lines.extend(f"- {x}" for x in evidence)
        uncertainty = str(result.get("uncertainty") or "").strip()
        if uncertainty:
            lines.append(f"UNCERTAINTY: {uncertainty}")
        sources = self._clean_list(result.get("sources", []))
        if sources:
            lines.append("SOURCES:")
            lines.extend(f"- {x}" for x in sources)
        if self.config.get("return_debug_meta", False):
            lines.append("META: " + json.dumps(result.get("meta", {}), ensure_ascii=False))
        if str((result.get("locked_facts") or {}).get("IMAGE_STATUS") or "") == "REQUIRED":
            lines.append(
                "INSTRUCTION_TO_MAIN: 当前 Tool Call 没有图片。请直接让用户重新发送/附上图片；"
                "不要从历史上下文、temp 目录、文件列表或旧消息猜图，也不要自行绕过 Pipeline 识别旧图。"
            )
        else:
            lines.append(
                "INSTRUCTION_TO_MAIN: 直接回答用户。LOCKED_FACTS 属于代码锁定事实，引用时必须逐字复制，"
                "不得纠错、同义改写或重新识别；若 IDENTITY_STATUS=UNCONFIRMED，严禁从 EVIDENCE/候选中"
                "自行推断具体身份；不要重复执行内部搜索。"
            )
        return "\n".join(lines)

    def _debug(self, stage: str, payload: Any) -> None:
        if self.config.get("debug_log", False):
            logger.info(
                "[vision-pipeline][%s] %s",
                stage,
                self._truncate(json.dumps(payload, ensure_ascii=False), 7000),
            )
