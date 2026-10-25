# 实施进度与交接（v0.8.0）

本文件记录《大肥鱼B站阅读视觉与认识成长_施工方案》的落地情况，以及 v0.4.0 的「自我成长」增量、v0.5.0 的 M5「自主探索整合」、v0.6.0 的「只读评论有限采集」、v0.6.1 的「置顶评论 + 楼中楼 UP 回复」、v0.7.0 的「只读弹幕抽样」、v0.8.0 的「本机 CPU+INT8 按需转写（ASR）」。只写已经存在的东西；离线验证与真实联网验证分开写，不混用。

## 1. 阶段与能力

| 里程碑 | 状态 | 说明 |
| --- | --- | --- |
| M0 基线与能力体检 | `verified_live`（本机） | 读完全部旧代码与 AstrBot 4.29.1 源码；确认 `context.llm_generate(image_urls=...)` 是官方真图片入口。解码器体检改用**已有二进制**：`04_语音\bin\ffmpeg.exe`（ffmpeg 7.1-essentials static，87,638,016B），未装新软件、未改 PATH；同目录无 ffprobe（`probe_duration` 退化返回 0，不影响抽帧） |
| M1 分P/字幕时间轴/分页 | `implemented_offline` + 真实接口连通 | `subtitle(bvid, cid)` 保留真实 `from/to`；`feiyu_read_bilibili(video, page=1)`、`feiyu_read_more(cursor)` 分页且不重复；游标带 HMAC 签名，换会话或篡改即拒。真实 API 已连通：无 Cookie 时返回 `reason=login_required`（AI 字幕需登录），结构化不报错 |
| M2 受限取流 + 安全抽帧 | `verified_live`（本机） | 官方 `playurl`（qn=32/fnval=16，≤480p，**按码率预算选档**）→ 无 Cookie 下载、域名白名单 + 443 + 私网解析拒绝 + 32MiB 上限 + sha256 → 隔离子进程 ffmpeg 逐帧抽 JPEG。真实跑通见第 5 节 |
| M3 AstrBot 真实视觉闭环 | `verified_live`（本机+云端容器） | `images_submitted` 语义已实现：`llm_generate(..., image_urls=[本地 JPEG 路径])`；一次最多 4 张；观察结果落 `visual_batches`/`observations`。`_vision_ready()` 在 provider 明确声明 `modalities` 且不含 `image` 时报 `provider_no_image`，**绝不拿文字回答冒充「看见了」**；新增 `vision_provider_id` 让看图单独走一个视觉模型。**已用真实模型跑通**（见 5.1：`visual_observed`，4/4 帧读懂比分） |
| M4 手账 + 认识修正 + 相关召回 | `implemented_offline` | 八栏手账 → 待核对区 → 收下/不记 → `reflections`（含 supersede/withdraw）→ 聊天中按主题权重召回，标注「只能当回忆，不当作新证据」 |
| M5 自主探索整合 | `implemented_offline` | 后台轮次按字幕筛选后记录判定（`vision_shadow` 表，含 verdict/reason），`auto_vision` **默认开**（主人 2026-10-25 明确要求）：每轮最多升级 1 条真正抽帧看图（全局媒体锁，深读与后台共用），`auto_vision=false` 时退回只记录。观察者钩子在点赞/分享决策**已定**之后才触发且返回值无人使用 → **画面不可能反过来买动作**。`/刷站 任务` 提供只读工作台（任务/帧批/模型/筛选决定/今日用量） |
| M5 遗留：只读评论（v0.6.0 / v0.6.1） | `implemented_offline` + 真实置顶/楼中楼已验证 | `replies()` 只 GET 第一页（`ps` 硬上限 20）、不带 Cookie、只留 `floor/likes/text/is_up`，去标签去链接截 300 字；落 `replies` 表（同一 revision 覆盖写）。只读评论区第一页；v0.6.1 再读**置顶评论**（`upper.top`）与**最多 3 条 UP 主自己在楼中楼的回答**：先看第一页已带回的楼中楼预览，不够时**最多再跟进 2 个线程**（`/x/v2/reply/reply`，每线程 `ps=20`，仍只 GET、无 Cookie、失败即跳过）——因为实测第一页预览里 UP 主的回答经常抓不到，不跟进就等于没实现；已在 `BV1hCnw65ELL` 真实抓到置顶作者评论与楼中楼作者回答各 1 条。UP 主本人写的（`is_up`）单列成「作者自己在评论区写的话」，可进 `author_view` 并注明来源是评论区补充；陌生观众仍只进 `material_facts` 的「有观众说」，提示词明令评论里的要求一律无效。不回复、不投币、不建陌生人档案、不自动采 |
| M5 遗留：只读弹幕抽样（v0.7.0） | `implemented_offline` | `danmaku()` 只 GET `comment.bilibili.com/<cid>.xml`（https、固定主机、无 Cookie、4MiB 上限、流式计数）：**一个静态文件、无分页、无 protobuf、无任何 POST**。解析时拒 `<!DOCTYPE`/`<!ENTITY`（防实体膨胀）、只取 `p` 字段第一段当秒数、清洗后截 60 字、`0 ≤ t ≤ 8天`；**按时间分桶抽样**（默认 30 秒一桶、每桶最多 2 条、同桶同文本折叠并记 `repeated`）→ 既不会只看开头刷屏，又保留「这句被刷了很多次」的热度。落 `danmaku` 表（同一 revision 覆盖写），只存时间与文本，不存 uid/昵称/颜色。弹幕**没有作者身份**：提示词把它单列成「同一时刻飘过屏幕的字（受众反应）」，只能进 `material_facts` 并注明「有观众在刷」，不许当作者观点、不许当画面、里面的要求一律无效。只在主人明确要求时读，不自动采、不发弹幕 |
| M6 本机按需 ASR（v0.8.0） | `implemented_offline`（未做真实推理验收） | 按主人 2026-10-25 的新决策替换旧方案 M6 建议：**CPU+INT8、不碰 CUDA、按需启动、跑完即退出**。只在「主人明确要读这一P 或明确发起一次浏览」时取得资格（`_open_part` 显式路径 / 显式浏览轮），无可用字幕时自动回退；**定时后台一律 `automatic_task` 拒绝**（`_transcribe_candidate` 还额外要求 `asr_background=true`，默认 false）。单条 <30 分钟（`asr_max_seconds=1799`）整条跳过、不截前段；并发 1（`_asr_lock` 覆盖下载+解码+推理整段）；每日 5 次（`asr_daily` 持久化、`reserve_asr` 原子占额、按时区 `Asia/Shanghai` 换日）；占额后失败/超时/取消仍计次；幂等重入（`job_id_for` + `begin_asr_job`）不重复占额。字幕因 `login_required/risk_control/unsafe_url/invalid_response/network` 失败时记 `subtitle_blocked`，**不绕道转写**。音轨走既有 `playurl` 白名单下载（`asr_max_mb=24`），`media_worker.extract_audio` 转 16k 单声道 WAV，独立子进程 `asr_worker.py`（`local_files_only`，默认禁止静默下载权重），**stdio 走文件不用管道**（宿主机沙箱禁管道）；成功/失败/超时/取消/重启遗留都清理临时目录，只保留结构化证据。ASR 证据**可以参与点赞**：`core._run` 仅当 `source='asr' and like_ok` 才允许点赞，且**从不强制点赞**、**绝不放宽分享** |
| M7 自我成长（v0.4.0 追加） | `implemented_offline` | 认识入户（`kind=fact` + `epistemic_type=derived`，带 evidence 与 op_key，可墓碑）、兴趣权重、提议-批准流程。**她可以提议，但主人点头才读；不自动读、不自动花模型钱** |

**一句话现状**：本机与云端都能真取流、真解码、真出图、真看图；后台自主轮次「先筛后看」，`auto_vision` 已按主人要求默认打开，每轮最多真的看 1 条；评论区只读第一页少量公开评论（外加置顶与最多 3 条 UP 主楼中楼回答），作者本人的话单独标注、陌生观众只当受众说法；弹幕只按时间分桶抽样，永远只当观众反应；主人明确要读而这一P没有字幕时，可以在本机用 CPU+INT8 小模型把音频转成文字（每日 5 次、单条 <30 分钟、并发 1、跑完即退出并删音频），机器转写只是证据、不当官方字幕也不当画面。

## 2. 改动文件

新增：

- `reading.py` —— 目标解析、帧时间计划、覆盖度、开读、分页渲染。
- `reading_store.py` —— 新表与游标签名（与 `core.Store` 同库共存，不改旧表）。
- `media_worker.py` —— 解码器定位、JPEG 尺寸解析、隔离子进程抽帧。
- `vision.py` —— 组批、提示词、JSON 解析、真图提交。
- `journal.py` —— 八栏手账提示词与解析、相关召回打分。
- `growth.py` —— 兴趣主题、想读提议、把「收下的认识」写成派生记忆视图（含撤回）。
- `asr.py` —— 转写的纯逻辑：资格闸门、时长校验、每日计数键、证据质量评估、提示词与报告渲染、worker 参数与环境、输出解析。**不 import 任何推理引擎**。
- `asr_worker.py` —— 真正加载模型的独立子进程：一次一条音频，转写完就退出；默认 `local_files_only`，绝不在任务里偷偷下权重；拒绝非 cpu 设备。
- `tests/test_reading.py`(21)、`tests/test_vision.py`(9)、`tests/test_journal.py`(12)、`tests/test_media_worker.py`(12)；
  工作区级 `05_B站浏览/tests/test_reading_plugin.py`(19)、`test_growth.py`(15)、`test_growth_plugin.py`(10)、`test_m5.py`(19)、`test_comments_plugin.py`(10)、`test_danmaku_plugin.py`(8)。

修改：

- `bili_client.py` —— `detail()` 增 `parts`/`aid`/`owner_mid`；`subtitle()` 改恒定六键并保留真实时间轴；新增 `playurl()`/`videoshot()`/`download_media()`/`_assert_public()`；v0.6.0 新增只读 `replies()` 与清洗函数 `_comment()`；v0.6.1 增 `_reply_item()`/`_reply_fingerprint()`/`_mid_number()` 与 `NESTED_UP_LIMIT=3`，`replies()` 收置顶 + 楼中楼作者回复；v0.7.0 新增只读 `danmaku()`/`_danmaku_xml()`/`_empty_danmaku()` 与模块函数 `_parse_danmaku()`/`_sample_danmaku()`（含 `_DANMAKU_HOST`/`_DANMAKU_MAX_BYTES`/`_DANMAKU_NODE_LIMIT`/`_DANMAKU_BUCKET_MS` 常量）；v0.8.0 新增 `audio()`（只取官方 dash 音轨、按码率取最小档、走同一 `_stream_urls` 白名单）。
- `main.py` —— 注册 0.8.0；`_llm` 支持 `image_urls`；新增 `_open_part`/`_look`/`_format_page`/`_write_journal`/`_purge_frames`；新增 7 个工具；`/刷站` 增 读/深读/手账/收下/不记/认识/更正/忘掉/提议/不读/评论/弹幕/任务；v0.5.0 再增 `_vision_verdict`/`_record_shadow`/`_maybe_auto_look`，`_look` 改薄包装 `_look_locked` 并共用 `_media_lock`；v0.6.0 增 `_read_replies`/`_format_replies` 与 `REPLY_REASONS`；v0.7.0 增 `_read_danmaku`/`_format_danmaku` 与 `DANMAKU_REASONS`；`状态` 报解码器、待核对手账、认识条数、兴趣条数、待点头提议数、评论与弹幕开关；`browsing_memory` 注入阅读指引、兴趣倾向、评论与弹幕边界、相关回忆；v0.8.0 注册 0.8.0，`_open_part` 改返四元组并在显式无字幕时触发转写，新增 `_asr_ready`/`_cleanup_asr`/`_kill_worker`/`_asr_outcome`/`_transcribe`/`_asr_run`/`_transcribe_candidate`/`_transcribe_explicit`/`_format_asr` 与 `ASR_BLOCKED_SUBTITLE`/`ASR_FALLBACK_SUBTITLE`，`/刷站` 增 转写/听写/听，新增工具 `feiyu_transcribe_bilibili`，`_run` 挂 `transcribe=self._transcribe_candidate`，`_write_journal` 传 `asr=`，`状态` 与 `任务` 报转写开关/引擎/今日次数。
- `_conf_schema.json` —— 45 键（v0.8.0 增 14 个 ASR 键： `asr_enabled`/`asr_background`/`asr_model`/`asr_compute_type`/`asr_language`/`asr_threads`/`asr_daily_limit`/`asr_max_seconds`/`asr_max_mb`/`asr_download_timeout_s`/`asr_decode_timeout_s`/`asr_worker_timeout_s`/`asr_model_dir` 等）：`auto_vision_shadow`、`auto_vision`（默认 true）、`vision_enabled`、`auto_vision_frames`、`deep_read_frames`、`media_max_mb`、`frame_max_kb`、`media_timeout_s`、`frame_keep_hours`、`ffmpeg_path`、`ffprobe_path`、`growth_memory_sync`、`proposal_limit`、`comments_enabled`、`comments_limit`、`danmaku_enabled`、`danmaku_limit` 等。
- `journal.py` —— `build_prompt(..., replies=...)` 分两段：`## 作者自己在评论区写的话`（UP 主置顶/楼中楼回答，可进 `author_view`）与 `## 观众在评论区说的话`（受众说法，只进 `material_facts`）；v0.7.0 增 `danmaku=` 参数与第三段 `## 同一时刻飘过屏幕的字（弹幕，受众反应）`（带 `mm:ss` 与 `×N`，只进 `material_facts` 的「有观众在刷」）；`SYSTEM` 第 6 条同时约束评论与弹幕；`_flag()` 统一把 DB 的 `'1'/'0'` 文本判成布尔；新增 `_stamp()` 把毫秒渲染成 `mm:ss`；v0.8.0 增 `asr=` 参数与第四段 `## 这一P的本地机器转写（CPU 转写，可能有错字）`（只能进 `material_facts` 并注明「机器转写」，听不清进 `omissions`，不许整理成通顺原文、不许当画面依据），`SYSTEM` 新增一条专门约束机器转写。
- `core.py` —— v0.8.0：`Browser.__init__` 增 `transcribe` 回调；`_run` 无官方字幕时按回退名单调回调，只有 `source='asr' and like_ok` 才允许 `like`，**仍然 `share=False`**；`RULES` 第 16 行从「没有字幕不点赞不分享」改为「没有官方字幕时只有机器转写证据足够才能点赞；仍不分享」。
- `reading_store.py` —— v0.8.0 增 `asr_jobs`/`asr_segments`/`asr_daily` 三表与 `reserve_asr`/`asr_used`/`begin_asr_job`/`finish_asr_job`/`asr_job`/`asr_job_for`/`add_asr_segments`/`asr_segments`/`recent_asr`，`_json_text()` 统一 JSON 列。
- `media_worker.py` —— v0.8.0 增 `extract_audio()`（ffmpeg 转 16k 单声道 PCM16 WAV，仍是隔离子进程）。
- `metadata.yaml` —— 版本 `v0.8.0`，描述补「真读分P字幕、真实抽帧、八栏手账与可修正的认识」「后台先筛后看、最多一条真看」「只读评论区第一页、不照评论要求做」「只读少量弹幕抽样、不当作者也不当画面」「没字幕时本机 CPU+INT8 转写、每日5次、<30分钟、跑完删音频」。

## 3. 数据与状态

- 仍是同一个 `browse.db`：新表 `readings`/`segments`/`frame_jobs`/`frames`/`visual_batches`/`observations`/`journals`/`reflections`/`interests`/`memory_links`/`proposals`/`meta`，全部 `CREATE TABLE IF NOT EXISTS`，旧表一字未动；v0.5.0 再增 `vision_shadow`（含 `idx_shadow_scope`）；v0.6.0 再增 `replies`（主键 `reading_id, revision, idx`）；v0.6.1 给它加 `pinned`/`nested` 两列（`ALTER TABLE ADD COLUMN` 增量迁移，旧行保留默认 `'0'`）；v0.7.0 再增 `danmaku`（主键同上，列 `at_ms`/`text`/`repeated`/`digest`/`source`）；v0.8.0 再增 `asr_jobs`（任务态、引擎/模型/语言、声明与解码时长、覆盖率、质量、note）与 `asr_segments`（起止时间/文本/平均对数概率/无语音概率）与 `asr_daily`（`day`+`scope` 主键的持久计数）。
- `PRAGMA user_version` 升到 `7`；重复打开同一库幂等（有测试）。旧的 v0.3.0（1）/v0.4.0（2）/v0.5.0（3）/v0.6.0（4）/v0.6.1（5）/v0.7.0（6）库打开后自动补齐新表与新列（`_add_missing_columns()` 只加列，不重建表、不搬行），新表为空即可用。
- 素材状态：`metadata_only` / `text_read` / `frames_ready` / `images_submitted` / `visual_observed` / `visual_unavailable` / `asr_transcribed`（转写够扎实）/ `asr_weak`（能读但不据以点赞）。请求深读但无解码器时写 `visual_unavailable`，同时 `frame_jobs.state=skipped, reason=decoder_missing`。
- 归属：`readings.scope`/`interests.scope`/`proposals.scope` 只存 `sha256(umo)[:32]`，不存明文会话标识；跨会话读写一律拒绝。
- 帧文件默认留 72 小时后由 `_purge_frames()` 删除（每小时最多一次），只留文字证据。
- 后台筛选决定的正交状态：`would_look`（本来会看）/`upgrading`（正在抽帧）/`looked`（已看）/`look_failed`（没看成）/`skip`（跳过，reason 记 `vision_off`/`decoder_missing`/`no_text`/`not_interested`）。`skip` 也留痕，便于主人审「为什么没看」。
- 评论只留 `floor`/`likes`/`text`/`is_up` 与来源，不存 uid、昵称、头像；同一 revision 重读评论是覆盖写，不会堆叠。评论只作手账 `material_facts` 的「受众说法」，不进 `author_view`、不当作她的推测。
- 弹幕只留 `at_ms`/`text`/`repeated` 与来源，不存 uid、昵称、颜色、字号、弹幕类型；同一 revision 重取也是覆盖写。`repeated` 是同一时间桶内同一句话被刷的次数（抽样时折叠成一条），既保留热度又不塞几百份重复文本。弹幕只作 `material_facts` 的「有观众在刷」，永远不进 `author_view`、也不当画面。
- 长期记忆写入：只写 `epistemic_type="derived"`，内容前缀「我自己整理的认识（不是主人的说法）：」；`fact_key=feiyu.bili.view:<主题>` 让同一主题的更正走 `auto_supersede`；`evidence[0].kind="bilibili_reading"`、`op_key=bili-view:<手账id>` 保证幂等；撤回认识时调 `forget_record` 写墓碑并断链。**推断内容绝不入库**（记忆插件本身也拒绝 `inferred`）。

## 4. 精确测试命令与环境结果

工作机 Python（无 aiohttp 依赖，可跑除 `test_bili_client` 外全部）：

```
C:\Users\16pro\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe -B <测试文件>
```

`test_bili_client.py` 需要 aiohttp，用 AstrBot 自带环境：

```
D:\DSH\QQBot\astrbot-tools\astrbot\Scripts\python.exe -B test_bili_client.py
```

最近一次全绿（2026-10-25，v0.8.0 本机 ASR 接入后）：

| 测试 | 结果 |
| --- | --- |
| 插件内 test_bili_client | 48 OK |
| 插件内 test_reading | 21 OK |
| 插件内 test_vision | 9 OK |
| 插件内 test_journal | 12 OK |
| 插件内 test_media_worker | 12 OK |
| 插件内 test_asr | 44 OK（含 5 个远程执行端契约） |
| 插件内 test_bili_login | 26 OK |
| 插件内 test_exploration | 12 OK |
| 工作区 test_core | 38 OK |
| 工作区 test_autonomy | 23 OK |
| 工作区 test_plugin | 36 OK |
| 工作区 test_reading_plugin | 19 OK |
| 工作区 test_growth | 15 OK |
| 工作区 test_growth_plugin | 10 OK |
| 工作区 test_m5 | 19 OK |
| 工作区 test_comments_plugin | 10 OK |
| 工作区 test_danmaku_plugin | 9 OK |
| 工作区 test_asr_plugin | 21 OK |
| 工作区 test_asr_remote | 7 OK |
| 合计 | **391 OK** |

## 5. 真实联网验收（2026-10-25，本机）

脚本 `D:\DSH\让大肥鱼活起来\_tools\live_accept.py`（需 aiohttp，用 AstrBot 自带 Python 跑），产物 `_tools\live_accept.json` 与 `_tools\live\shown\frame_*.jpg`。

实测对象：`BV1HiHN6zETj`（1P，753s，cid 43019799288），首页推荐自动选中。

| 检查 | 真实结果 |
| --- | --- |
| `detail()` | 标题/作者/时长 753s/cid/pages=1/`parts[0]` 全部有值 |
| `subtitle()` | 六键齐全，`source=none`、`reason=login_required`（无 Cookie 时 AI 字幕必然要登录）；不抛异常、不伪造时间轴 |
| `playurl()` | `kind=dash`，可用档 4 个（480p=39.9/76.2MiB 估计，360p=26.8/59.6MiB 估计），另有 durl 兜底 |
| 预算选档 | 32MiB 上限下自动跳过 480p、选 360p（估计 26.8MiB），实际下载 **28,045,001B** |
| `download_media()` | 无 Cookie 下载成功；返回 `sha256` 与本地重算一致（`565afff2ebda5011`），域名白名单/443/私网拒绝全过 |
| `MediaWorker.extract()` | ffmpeg 7.1 子进程出 **4 张 640×360 JPEG**（24,445 / 27,576 / 33,416 / 26,332B），`source=ffmpeg` |
| 帧时刻 | 计划 94125/282375/470625/658875ms → 实际 94133/282400/470633/658900ms（`-ss` 偏移已校正） |
| 画面真实性 | 人工看图确认是画面内容（球场/比分层/看台），不是黑帧或占位图 |
| AstrBot 媒体管线 | 用 AstrBot 自带的 `resolve_image_ref_to_base64_data()` 解析这 4 个本地 JPEG：全部得到 `image/jpeg` 的 base64（32,596 / 36,768 / 44,556 / 35,112 字符），说明 `llm_generate(image_urls=[本地路径])` 这一段在生产 AstrBot 里确实能把帧变成图片块（返回值是 `ResolvedMediaData(base64_data, mime_type)` 对象，不是字符串） |

过程中修掉的三个真实缺陷（都有测试兜底）：

1. **沙箱不能建匿名管道** → `media_worker` 全面改文件承载 stdio（`_run(args, timeout, log_path=...)`），生产侧也更不容易死锁。
2. **`actual_ms` 报 8/25ms** → 输入 seek 会重置时间轴，真实时刻 = 目标 + `pts_time` 偏移（限 [-1,5] 秒内才采信）。
3. **480p 对 12 分钟视频超 32MiB** → `playurl` 返回全部 ≤480p 档位（`tiers`），`reading.pick_tier()` 按 `bandwidth×duration/8 ≤ 0.9×上限` 选最高能装下的档；选不到才退最小档（4 个新测试）。

## 5.1 真实多模态验收（2026-10-25，云服务器容器内）

把插件自己的 `vision.py` 与上面 4 张真实帧一起送进云端 AstrBot 容器跑了一遍真请求（脚本 `_tools/cloud_vision_pipeline.py`，密钥只在容器内读取、不打印）：

| 检查 | 真实结果 |
| --- | --- |
| 视觉模型 | `xiaomi/mimo-v2.6-flash`（云端 `modalities=[text,image,audio,tool_use]`，并且是 `default_image_caption_provider_id`） |
| 请求 | `vision.build_prompt()` 生成 289 字提示 + 4 张 JPEG，`images=4` |
| 结果 | `state=visual_observed`、`reason=''`，四条观察全部 `visible=True` |
| 模型读到的内容 | 比分演进 AVL 1-0 → 1-1 → 2-1、场次时间 06:45/30:38/55:28、看台 "UP THE VILLA"、"VISIT RWANDA" 广告牌、场边光头教练；对教练身份主动标 `uncertainty=教练具体身份无法确认` |
| usage | `image_tokens=880`、`reasoning_tokens=316`，说明图**真的作为图像被编码**，不是当文本吞掉 |

对比测试：同一张合成图（CODE 7391）发给两家 —— `mimo-v2.6-flash` 正确答出 7391（`image_tokens=44`）；`deepseek-flash` 的 `reasoning_content` 里也引用了 7391（说明它也看到了图），但 900 token 全烧在思考上、`content` 仍为空。**结论：看图首选 mimo，deepseek-flash 会把预算耗在推理上。**

因此新增配置 `vision_provider_id`：聊天仍用原模型，抽帧看图单独走一个视觉 provider。云端实测配置值是 `xiaomi/mimo-v2.6-flash`。

## 5.2 生产部署与生产侧验收（2026-10-25 17:37 CST，容器 `astrbot`）

已部署 v0.4.0 到 `/opt/qqbot/astrbot/data/plugins/astrbot_plugin_feiyu_bili` 并重启 AstrBot。部署前整包备份在 `/opt/qqbot/feiyu-bili-backups/20261025-173630-v030-pre`（plugin/config/plugin_data 三份，880K，权限 700）。

| 检查 | 生产实测 |
| --- | --- |
| 加载 | `Loading plugin astrbot_plugin_feiyu_bili ...` → `Plugin ... (v0.4.0)`；`[feiyu-bili] 自主探索就绪 auto=False likes=True bound=True decoder=True` |
| 解码器 | 容器内自带 `ffmpeg 7.1.5-0+deb13u1` 与 `ffprobe`（`/usr/bin`），`decoder=True`，**不需要另装** |
| 库迁移 | `PRAGMA user_version` 0 → 2；新增 readings/segments/frame_jobs/frames/visual_batches/observations/journals/reflections/interests/proposals/memory_links/meta，旧六表原样保留 |
| 错误 | 重启后 `feiyu_bili` 相关 error/traceback：0；全容器 traceback 计数 0 |
| 真实字幕 | 带生产凭证：`subtitle_state=timed`，**202 段**，覆盖比 0.84，首段 6700–7100ms；无 Cookie 时同一接口是 `login_required` → 说明生产 Cookie 生效 |
| 真实取流 | dash 4 档（480p 849/444 kbps、360p 664/298 kbps），预算选档后下载 28,045,001B，sha16 与接口一致 |
| 真实抽帧 | 4 张 640×360 JPEG（24,445/27,576/33,416/26,332B），实际帧时刻 94133/282400/470633/658900ms |
| 真实看图 | `mimo-v2.6-flash`：`state=visual_observed`、4/4 可见、`image_tokens=880`；读出 1-0 → 1-1 → 2-1、06:45/30:38/55:28、UP THE VILLA / VISIT RWANDA，并对模糊处自报不确定 |
| 数据安全 | 验收用 `_tools/prod_e2e.py` 在 `/tmp` 里的临时库跑，未写入生产 `browse.db`；凭证只被 `BiliClient` 读取、未打印；签名媒体 URL 不落库、不打日志 |

尚未在本页记录的最后一跳：**QQ 对话里**发起一次 `/刷站 深读 <BV>`，验证 AstrBot `llm_generate` 传输层（配置已指到 mimo）。

## 5.3 只读弹幕真实接口核验（2026-10-25，本机联网，无 Cookie）

脚本 `_tools/probe_danmaku.py`（产物 `_tools/probe_danmaku.json`）与 `_tools/probe_danmaku_parts.py`（产物 `_tools/probe_danmaku_parts.json`），都用 AstrBot 自带 Python 跑；只读、不带 Cookie、不落库、不打印任何 uid/昵称。

| 检查 | 真实结果 |
| --- | --- |
| 5 个单P视频 | 全部 `HTTP 200`、`Content-Type: text/xml`、**`Transfer-Encoding: chunked`（没有 `Content-Length`）**、无重定向 |
| 响应体形状 | `<?xml version="1.0" encoding="UTF-8"?><i><chatserver>chat.bilibili.com</chatserver><chatid>CID</chatid><mission>…</mission><maxlimit>…</maxlimit><d p="…">文本</d>…</i>` |
| `p` 字段 | 实测 **10 段**（`time,mode,size,color,ts,pool,hash,rowid,weight,?`），第 1 段确实是**浮点秒**；5 个视频 `bad_time=0`，最大值分别 1843.3/133.04/535.9/101.58/1271.48s ≡ 各自时长 |
| 体积 | 20KB–692KB（最大是 7200 条那条），离 4MiB 上限很远；`<maxlimit>` 存在，说明 B站只给上限内的子集 |
| `DOCTYPE`/`ENTITY` | 5 + 3 个真实响应体**全部没有**，我们的拒收规则不会误伤真实数据 |
| 插件解析 | 5 个视频全部 `reason=ok`，`items=8…40`；`total` 略小于 `<d>` 条数（如 1903 vs 1925），差的正是被丢弃的 1 字/空白条目 |
| 分P归属 | `BV1Eb411u7Fw`（195P）P1/P2/P3 的 cid 122452656 / 435050022 / 1263257643 各自 `chatid` 互不相同、条目 7200/600/976 → **弹幕确实按分P（cid）而不是按视频** |
| 空结果 | 不存在的 cid（999999999）：`HTTP 200` + 合法 `<i>` + 0 个 `<d>` + `<text_side>本视频弹幕功能已被关闭</text_side>`（275B） |

据此修的两个真实缺陷（都有新测试）：

1. **「没人发弹幕」与「弹幕功能被关闭」被混成一句话** → 新增 `bili_client._danmaku_closed()`，读到非空 `<text_side>` 就返回 `reason=closed`；`main.DANMAKU_REASONS['closed']='这个视频的弹幕功能已被关闭'`，`empty` 改为「这份弹幕文件里没有可解析的弹幕（可能还没人发）」。（`test_bili_client.py::test_danmaku_reports_closed_when_bilibili_says_the_feature_is_off`、`test_danmaku_plugin.py::test_closed_danmaku_says_the_feature_is_off`）
2. **`total` 会被当成「这条视频的弹幕总量」** → 原尾注「这一P的弹幕原始条目约 N 条」改为「这份弹幕文件里能解析出 N 条，B站给的本身就只是一个上限内的子集；这里只是按时间分桶后的抽样，不代表全站弹幕总量」。`BV1Eb411u7Fw` 的 P1 有 7200 条正是 `maxlimit` 截断的典型。

## 5.4 v0.8.0 ASR 的离线验证边界（2026-10-25）

2026-10-25 晚补记：**真实推理已经跑过**（本机 small / 云端 base+small），实测数字见 `docs/ASR_BENCH.md` 第 4、6 节与 `ACCEPTANCE.md` 的 B14/B17；本节保留的是「离线测试能证明什么」的清单，以及当初「没有实测就不编数字」的口径。

**权威执行端 = 本机（主人 2026-10-25 选定）**：插件留在云端，本机起 `_tools/asr_service.py` 跑 small；云端只发 `bvid`/`cid`，音轨只在本机下载解码转写，云端不接收任何音频字节。通道 = 反向 SSH 隧道（本机 `ssh -R 18080:127.0.0.1:18080`）+ 宿主 relay（容器 → `172.17.0.1:18081` → 宿主 `127.0.0.1:18080`）。

| 真实证据（2026-10-25） | 数字 |
| --- | --- |
| 本机 small，120 秒子片 | 推理 12.35s，RTF 0.103，峰值 RSS 580MB（4 线程 int8） |
| 本机 small，整条 752.4s | 推理 73.79s，RTF 0.098，峰值 833MB，149 段 2243 字 |
| 容器内发起 → 本机转写（跨机链路） | 往返 **78.0s**，149 段 2243 字，首段「好了 欢迎各位和记录一会说完这场英朝联赛的第八轮」 |
| 云端 1.7GB 跑 small | 会把 AstrBot 一起 OOM（`exitCode=137` 三次）；base 峰值 419MB / RTF 0.389 可跑但质量弱 |
| 云端 ≈ 480p 音轨 | 6,168,363B → 16k 单声道 WAV 752.4s（24MB），跑完即删 |

执行端配置：`asr_endpoint`（如 `http://172.17.0.1:18081`）、`asr_endpoint_token`（可选共享口令 `X-Feiyu-Token`）、`asr_remote_timeout_s`（默认 900）。留空则退回「插件所在机器跑 worker」。

| 已用离线测试证明 | 靠什么证明 |
| --- | --- |
| 显式无字幕才自动转写，定时后台一律拒绝 | `test_asr_plugin.py`（`automatic_task`、`asr_background=false` 默认）与 `test_asr.py` 的 `qualify()` 用例 |
| 有字幕时一次都不花额度 | 同上：`subtitle_available` 分支零下载、零占额 |
| 1799s 放行、1800s 与 3600s 整条跳过（不截前段） | `qualify()` 边界用例 + 插件级「提示里明说整条跳过了」 |
| 每日 5 次是硬上限：第 6 次拒绝、7 次并发只成 5 次、跨午夜归零、重启后仍在、幂等重入不重复占额 | `reserve_asr()` 的 `UPDATE ... WHERE used<?` 与 `begin_asr_job` 幂等，配上并发/跨日/重启用例 |
| 并发 1 真实生效（锁覆盖下载+解码+推理整段，不只是模型调用） | `_asr_lock` 包住 `reserve_asr` 与 `_asr_run` 的全部 I/O |
| 成功/失败/超时/取消都删临时目录，启动也清理遗留 | 每个终态都断言 `media/asr/<job_id>` 不存在；启动清理用例额外验证「不动目录外的文件」 |
| worker 只接受受控参数：只传路径与标量、无 shell 拼接、环境里清掉代理与 `CUDA_VISIBLE_DEVICES` | `asr.worker_args()`/`asr.worker_env()` 用例 |
| 弱证据（静音/纯音乐/重复幻觉/乱码/截断）不得点赞 | `assess()` 七类异常 + 核心层「弱转写不点赞」用例 |
| 合格转写可以让点赞成立，但**从不强制点赞**、**绝不放宽分享** | `core._run` 的 `source=='asr' and like_ok` 闸门 + `share` 恒 false 的用例（`sender.await_count==0`） |

| 离线证明不了、必须真实跑一次 | 说明 |
| --- | --- |
| `faster-whisper` 在本机/云端是否真的可用、版本与 int8 兼容性 | 只能实际安装后看 `--probe` |
| 单条 30 分钟音频的 CPU 耗时、峰值内存、线程数取值 | 需要真跑；目前配置默认 2 线程、`asr_worker_timeout_s=900` 只是超时护栏，不是实测值 |
| 中文清晰/噪声/专名/数字/否定句的实际识别质量 | 测试里用的是假 worker 的固定文本，只验证契约，不验证识别质量 |
| 与 AstrBot 同机时对 QQ 响应的实际影响 | 云端 small 已证明会 OOM，因此执行端放本机；本机转写对聊天的影响仍需部署后观察 |

15. ~~**v0.8.1：每日 10 轮 + 出错不再停摆**~~ → **已上线（2026-10-25 21:15 实测通过）**：

    - 主人要求「每天十轮」：`daily_rounds` 默认 8 → **10**（硬上限仍 16），代码两处默认值与 `_conf_schema.json`、生产配置同步改。
    - 顺手查出并修掉一个真 bug：`main._run` 把 `transcribe=` 传给 `Browser.run()`，而 `Browser` 只在构造时接受它 →
      `TypeError: Browser.run() got an unexpected keyword argument 'transcribe'`，**每一次后台自主轮次都会失败**。
      离线测试没抓到，是因为那些用例把 `browser` 换成了 Mock（Mock 接受任意关键字）。修法：`Browser.run` 接受 per-call `transcribe`。
      新增回归 `test_run_accepts_a_transcribe_callback_without_mutating_like_gates`（真实 Browser + 假字幕空 → 回调被调用、`liked=0`）。
    - 自主循环不再「一次错误就关一整天」：失败先退避 30 分钟，**连续 3 次**才把 `auto` 关掉；异常原文写进 `settings.last_error`，
      `/刷站 状态` 末行显示「最近一次自主活动失败：HH:MM TypeError: …」，日志只打异常类型（不把任意文本写进日志）。
    - 结果：`/刷站 状态` 会同时显示今日轮次（10 上限）与最近一次失败原因；线上实测 21:14:43 一轮 `status=ok read=3 liked=0 sent=0`，
      并伴随 `后台视觉 state=visual_observed frames=4`（真的抽了 4 帧看过）。

## 6. 未完成 / 需要主人批准

1. ~~生产环境装 ffmpeg~~ → **不需要装**：容器自带 ffmpeg 7.1.5 + ffprobe（`decoder=True`）。
2. ~~真实多模态请求~~ → **已完成**（见 5.1 与 5.2）。仅剩「走 AstrBot `llm_generate` 传输层」这一步：生产已部署并配好 `vision_provider_id`，在 QQ 里深读一条即可闭环。
3. ~~真实字幕验收~~ → **已完成**（见 5.2：202 段 timed）。
4. ~~生产部署~~ → **已完成**（v0.4.0 已上线，见 5.2 与 `ROLLBACK.md`）。
5. ~~M5 自动视觉的灰度~~ → **已按主人要求打开**（2026-10-25：`auto_vision` 默认 true）。仍保留 `auto_vision_shadow`：关掉 `auto_vision` 就退回只记录判定。打开后每轮最多升级 1 条、`auto_vision_frames` 帧（默认 4，方案建议 8–12）。
6. ~~v0.5.0 / v0.6.x / v0.7.0 生产部署~~ → **已完成（2026-10-25 20:35，直接上到 v0.8.0）**：备份 `/opt/qqbot/feiyu-bili-backups/20261025-201854-v040-pre-v080/`，部署后日志 `Plugin astrbot_plugin_feiyu_bili (v0.8.0)`、`auto=False likes=True bound=True decoder=True`；线上库 `user_version=7`。
7. **videoshot 快照来源**：已实现但默认没有调用入口；只有在主人明确批准后才会作为无解码器时的降级来源，且证据必须标 `source=videoshot` 与缩略图分辨率。
8. **自我成长的真实效果**：只在离线测试里验证过。真实链路上还要确认记忆插件版本支持 `add_record(evidence=..., op_key=...)`（本机 03_长期记忆 源码支持），以及长期库里不会因夜间整理把她自己的整理当成主人原话。
9. ~~只读评论的真实接口~~ → **已联网核验**（2026-10-25，无 Cookie，`BV1HiHN6zETj`）：`upper.mid` 为 int、评论者 `row.mid` 为 int、`member.mid` 是字符串、`floor` 已为 null；据此修掉「`is_up` 永远为假」与「楼层号恒 0」两处真缺陷并补回归测试。该覆盖缺口已在 v0.6.1 补上，并**在真实数据上验证过**：`BV1hCnw65ELL` 实跑 `replies(limit=8)` 返回 5 条，含 1 条 UP 主置顶（385 赞）与 1 条 UP 主楼中楼回答（428 赞），清洗全过；`_tools/probe_nested.py` 扫首页推荐 6 个视频、每个跟进前 3 个线程，命中 1 处 UP 主楼中楼回复，其余 0 是该 UP 主本来就没回。
10. ~~M6 可选 ASR~~ → **已按主人 2026-10-25 的新决策实现（v0.8.0，离线）**，旧方案里「需逐项点头」的问号由这次指令直接回答：CPU+INT8、按需启动、单条 <30 分钟、每日 5 次、并发 1、只留临时文件、允许 ASR 证据参与点赞但不放宽分享。**只剩真正跑一次真实推理与部署**（见第 12 条）。
11. ~~只读弹幕的真实接口~~ → **已联网核验**（2026-10-25，无 Cookie，见 5.3）：`comment.bilibili.com/<cid>.xml` 返回 200 + chunked 的合法 `<i>`，`p` 第 1 段确为浮点秒，真实体里没有 DOCTYPE/ENTITY，弹幕按 cid 分P。据此把「没有弹幕」与「弹幕功能已关闭」（`<text_side>`）分开、并改掉会误导的「原始条目数」措辞。**唯一仍未证明的是生产环境内的同一路径**（云端仍是 v0.4.0，不含弹幕代码）。
12. ~~v0.8.0 ASR~~ → **已上线（2026-10-25）**，三件事都已落地：
    - **执行位置（主人已定：本机跑 small，云端不跑模型）**：云端 `_asr/asr_env`（439MB）与 `models/`（三档权重 720MB）**已删除**，容器内 `import faster_whisper` 现在报 `ModuleNotFoundError`；`_asr/` 只剩 220K 脚本（relay + probe + bench）。云端内存回到 `available 689MB`、`astrbot` 容器 RSS 315MB，删除后**没有再出现 OOM kill**（`dmesg | grep -c "killed process" == 0`）。
    - **通道**：容器 `asr_endpoint=http://172.17.0.1:18081` + 32 位 `asr_endpoint_token`（`X-Feiyu-Token` 已强制，本机 `asr_service.py` 与配置里的口令一致）→ 宿主 `host_asr_relay.py`（`172.17.0.1:18081`）→ 反向 SSH 隧道（只绑云端 `127.0.0.1:18080`）→ 本机 `127.0.0.1:18080`。实测容器内 `curl -H "X-Feiyu-Token: …" http://172.17.0.1:18081/health` → `{"ok": true, "engine": "faster-whisper", "model": "small", "busy": false}`；无口令返回 `{"ok": false, "reason": "forbidden"}`。
    - **开机自启**：云端 `/etc/systemd/system/feiyu-asr-relay.service`（`enabled` + `active`，`Restart=always`）；本机 `%APPDATA%\…\Startup\feiyu_asr_up.cmd` → `C:\Users\16pro\AppData\Local\feiyu_asr\up.cmd` 拉起 `service.cmd` + `tunnel.cmd` 两个循环（纯 ASCII launcher，中文路径从 GBK 的 `tools_path.txt` 读入）。
    - 仍然**只有**本机跑模型；云端插件只发 `bvid/cid`、只收回文字。真实单条 752.4s：本机 small 推理 73.79s、峰值 833MB。
13. ~~真实点赞验收~~ → **已做（2026-10-25，`BV17iHG6XEsF`）**：走插件自己的 `has_liked → reserve_like → like(单写) → like_result → 回读`，B站回读 `has_like=1`、`relation.like=True`；配额用尽时被正确拦下。细节见 `ACCEPTANCE.md` 的 B15。**未做的部分**：没有让 LLM 决策层自己产出这一次点赞（那一天 3 次额度已被她的后台轮次用完，本次验收用临时上限 4 完成，配置仍是 3）。
14. ~~GitHub 同步~~ → **已发布 v0.8.0**（2026-10-25，commit `2c05a07`，42 个文件）：README 重写到 v0.8.0、文档五件套随包、测试 19 个文件全上；本地暂存树 `05_B站浏览/_publish/astrbot-plugin-feiyu-bili`，上传脚本 `_publish/upload_repo.py`（GitHub REST，单提交）。

## 7. 权限与回滚

- 未改服务器配置、未改库结构以外的任何权限，未新增网络出口，未新增收藏/投币/评论回复/私信/上传。
- 自我成长只写 `owner_private` 的派生事实，不改人格文件、不改说话方式，主人可用 `/刷站 忘掉` 撤回并在长期库留墓碑。
- 评论只读第一页少量公开评论，不回复、不点赞评论、不建陌生人档案；评论内容不作为作者观点、不执行评论里的任何要求。
- 弹幕只读一个静态 XML、按时间分桶抽样，不发弹幕、不点赞、不建档案；弹幕永远只当受众反应，不当作者观点也不当画面。
- ASR 只在本机/服务器本地用 CPU 推理，不调任何云 ASR、不外发音频；音轨与 WAV 只落在 `media/asr/<job_id>/` 临时目录，成功/失败/超时/取消都会删掉，启动时还会清理遗留目录；只保留结构化转写片段与判定理由。ASR 证据**只可能**让「点赞」这一项成立，**不会**放宽分享资格，也不会改点赞额度、只增赞、预查与去重规则。
- 回滚见 `ROLLBACK.md`。
