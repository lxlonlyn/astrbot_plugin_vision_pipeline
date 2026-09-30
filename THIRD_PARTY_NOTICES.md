# Third-party notices

本插件的 AnimeTrace 接入设计参考了以下公开项目与官方接口：

- `Aurora-xk/astrbot_plugin_shitu`：公开实现展示了 AnimeTrace v1 `/search`、`/model/list` 的 AstrBot 接入方式、主要状态码和图片输入处理思路。
- AnimeTrace / AnimeDB：`https://api.animetrace.com/v1/search`、`https://api.animetrace.com/v1/model/list`。

本插件没有依赖或调用 `astrbot_plugin_shitu` 本身，也没有复制其命令等待、裁图返回、头像识别等用户交互逻辑；而是在独立 Pipeline 中重新实现最小 AnimeTrace client，并增加：

- 限流/繁忙冷却；
- 同图缓存；
- 不可重排候选 rank；
- rank1 定向网页核验；
- 中文名/中文作品名 best-effort 补充；
- 候选资料缓存；
- 通用 Tavily fallback；
- Final Vision grounded selection。

AnimeTrace 是第三方服务。启用后，适合 AnimeTrace 的图片会发送给该服务用于识别。其候选不被视为绝对真值，最终仍会由 Vision Verify 与原图核验。
