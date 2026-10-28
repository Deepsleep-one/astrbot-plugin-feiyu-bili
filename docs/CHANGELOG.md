# 更新日志 · astrbot_plugin_feiyu_bili

大肥鱼在 B 站的「读、看、记、想」插件。本文件按版本记录演进，供以后查「这个功能是哪一版加的」「这个坑当时怎么修的」。
插件当前版本见 `metadata.yaml`；能力清单见 [`MANUAL.md`](MANUAL.md)，验收证据见 [`ACCEPTANCE.md`](ACCEPTANCE.md)。

记录口径：

- 日期为北京时间（Asia/Shanghai）。同一天的多个版本按时间先后排列。
- 「测试」指离线用例总数（`unittest`，逐个文件跑；`test_bili_client.py` 需要带 aiohttp 的解释器）。
- **只记录有据可查的版本**。中间有几版是同一天连续迭代、没有单独发 release，这里仍然逐版列出，因为代码与测试确实停在过那些版本上；`v0.9.0` / `v0.9.1` 没有留下独立记录，0.9 系列首个对外版本是 `v0.9.2`。

## 版本一览

| 版本 | 日期 | 一句话 | 测试 | 上线 |
|---|---|---|---|---|
| v0.2.1 | 10-25 08:01 | 自主探索、独立点赞、低频 QQ 分享（起点） | 169（当时口径） | 云端 |
| v0.3.0 | 10-25 上午 | 能真读一条指定视频：字幕时间轴、抽帧看画面、八栏手账、可修正的认识 | 232 | 代码（随 v0.4.0 首上云端） |
| v0.4.0 | 10-25 17:37 | 自我成长：兴趣权重、想读清单（主人点头才读）、认识写进长期记忆 | 257 | 云端 |
| v0.5.0 | 10-25 傍晚 | 后台先按字幕筛，默认只记「本来会看哪一条」；只读工作台 | 286 | 代码 |
| v0.6.0 | 10-25 | 只读评论区第一页（观众说法，不是作者观点） | 297 | 代码 |
| v0.6.1 | 10-25 | 认得出「UP 主自己的评论」（含置顶与楼中楼回答） | 304 | 代码 |
| v0.7.0 | 10-25 | 弹幕抽样：按 30 秒分桶、同句折叠成 ×N | 317 | 代码 |
| v0.8.0 | 10-25 20:35 | 没字幕时用本机 CPU+INT8 转写（跨机隧道，音轨不出本机） | 391 | 云端 + 本机 |
| v0.8.1 | 10-25 21:15 | 每天十轮；出错先退避、连续三次才停 | 395 | 云端 |
| v0.8.2 | 10-25 22:04 | 每天最多分享 5 条，且摊到全天 | 407 | 云端 |
| v0.9.2 | 10-26 14:55 | 只读桥 + 分享交接（分享改成写 outbox，插件自己不再发） | — | 云端 |
| v0.9.3 | 10-28 21:38 | 她终于能说清「今天刷了多少条」（系统计数注入） | 409 | 云端 |
| v0.9.4 | 10-28 23:29 | 手账吃全部字幕、刷站回报带标题与 BV、额度说清怎么算 | 424 | 云端 |

---

## v0.9.4 · 2026-10-28 23:29

**主题：修她自己报上来的四个问题（这次是她查出来的，不是猜的）。**

为什么改：她读完 83 条、到 03:46 才收尾，可 `keep_journal` 生成的手账里写着「字幕原文到 01:05 为止」，还把后两分钟当成「遗漏」。她说「当时我调用它时，它应该已经能拿到完整字幕了」——她说对了。

修复：

- **手账只吃到第一页字幕**（真实缺陷）。`_write_journal` 原本调 `render_page(..., offset=0)`，一页 24 条、2600 字，83 条里只有前 24 条进了 prompt，剩下的一律被模型写成「遗漏」。新增 `reading.transcript()`：取整P全部时间轴，超 14000 字才截（保留头部 75% + 尾部，中间插一条显式的「省略了 N 条，省略不等于视频没讲」），并返回 `total/first_t/last_t/truncated/dropped`。
- **不许把「材料截断」写成「视频没讲」**。`journal.SYSTEM` 加第 8 条：摘录到哪里只代表材料到哪里；材料里写了「省略了 N 条」时只能说「中间有一段我没拿到」。段落标题也从「字幕原文」改成「字幕原文（整P，第 n 条 · 时间）」，开头写明「这一P共 83 条时间轴（00:00–03:43）；下面是全部内容」。
- **刷站回报太干**。成功时原本只有「读了 1 条，点赞 0 条，分享 0 条」。新增 `main._round_report()`：逐条列 `标题 · BV 号 · 证据来源（官方字幕/机器转写/只有标题）· 字数 · 兴趣判断 · 点赞结果`，再给合计。`/刷站 刷` 与 `feiyu_browse_bilibili` 共用它。
- **`round_limit` 只有键名**。现在带 `cap`/`used` 并写明「已经刷了 X/10 轮，每天 00:00 按北京时间重置」，还说明「读指定视频不占这个轮次额度」。`busy` 也改成能读懂的句子。
- **防乱编前情**。`browsing_memory` 里加硬约束：说到「上次/之前看过」只能引用注入记录里真实出现过的标题与 BV 号，记录里没有就说没有具体记录，绝不编内容、数字或剧情。
- **顺带修一个会让工具静默降级的隐患**：`explore_tool` 原本在函数体里做 `from .exploration import safe_query`，一旦插件包只在模块 exec 期间存在（离线测试、或未来换加载方式），调用时就会 `ModuleNotFoundError` 并被吞成「这次浏览没有完成」。改为模块级导入。

变更：

- `core.Browser._run` 的 `result` 新增 `seen[]`（每条：`bvid/title/author/source/chars/segments/interested/liked/like_state/topic`），`round_limit` 返回 `cap`/`used`。这只是把已经发生的事实报出来，不改任何动作闸门。

测试：424 项（19 个文件）；新增 15 项——`plugin/tests/test_reading.py` 21→24、`plugin/tests/test_journal.py` 12→14、`tests/test_reading_plugin.py` 19→25、`tests/test_core.py` 39→42、`tests/test_plugin.py` 37→38。

上线：云端 v0.9.4，未改库结构（`user_version` 仍 7）。

## v0.9.3 · 2026-10-28 21:38

**主题：把「今天刷了多少」从推断变成系统计数。**

为什么改：她回答「今天刷了几条」时只能说「能确认的 3 条，敢复述的 1 条」。查下来不是模型的问题，是注入的材料里只有最近三条浏览明细、没有任何日累计，模型只能把明细当成全部。她不肯往数上凑是对的。

新增：

- `core.Store.daily_videos(day=None)`：按 `read_at` 前缀数当天视频条数。
- `core.Store.daily_stats(day=None)` → `{day, videos, interested, rounds, shares, likes}`。
- `main._tally_line()`：注入 `[今天的真实浏览统计（系统计数…不要把下面三条明细当成全部）] 今天已刷 X/10 轮，共读过 Y/30 条视频（其中 Z 条我判断有兴趣）；今天已主动分享 A/5 条、点赞 B/3 条。`，`browsing_memory` 命中后无条件追加。
- `feiyu_browse_bilibili` 的返回值补上当天累计，避免工具只回本轮计数。

修复：

- 版本号与 `metadata.yaml` 不一致（代码里 `@register` 写 `0.8.2`，元数据 `v0.9.2`）。AstrBot 读元数据所以没报错，但两处现已统一为 `0.9.3`。
- 一个日期型测试炸弹：`test_delivery_reservation_caps_at_five` 的断言读在 `patch.object(core, "now")` 之外，按真实日期取日，写它那天能过、三天后必挂。已移进 patch 内。

上线：`/opt/qqbot/feiyu-bili-backups/20261028-213735-v092-pre-v093` 备份后热更，日志 `Plugin astrbot_plugin_feiyu_bili (v0.9.3)`。容器内实跑 `daily_stats()` 得 `videos 27 / interested 20 / rounds 9 / shares 4 / likes 3`，与三条明细标题一致。

---

## v0.9.2 · 2026-10-26 14:55

**主题：只读桥与分享交接补齐。**

- 新增 `read_port` 只读接口：别的插件（或工作台）可以只读取数，不需要碰它的写路径。
- 分享改成**只写 `share_outbox`，B 站插件自己不再直接发 QQ**：真正发消息交给陪伴插件统一排程。启动日志会打 `分享交接已打开：分享只写 outbox，B站不再自己发`。
- 这一版之后，`IMPLEMENTATION_PROGRESS.md` 与 `MANUAL.md` 有一段时间没跟上（能力清单停在 v0.8.2），v0.9.3 时已在文档里加了滞后提示。

注：该版本的测试总数当时没有留档，v0.9.3 的 409 项包含了它带来的用例。

---

## v0.8.2 · 2026-10-25 22:04

**主题：每天最多分享 5 条视频，并且分布在全天。**

原来的毛病：额度是「当天没用完就一口气发」，早上连发完，晚上反而没得发。

新增（`exploration.py`，纯函数、可单测）：

- `share_allowance(daily, moment, quiet_start=23, quiet_end=9)`：从 09:00 起按小时解锁，阶梯 09→1、12→2、15→3、18→4、21→5；静默时段与 `daily<=0` 一律 0。边界是 `into >= length` 才算窗口关闭（23:00 整点算夜间）。
- `spread_rest(active_left, rounds_left, lo=30, hi=240)`：用「剩余可说话时间 ÷ 剩余轮次」算间隔。
- `align_to_window(stamp, ...)`：落在 23:00–09:00 的唤醒顺延到 09:00（夜里跑了也发不出去，白烧额度）。
- `active_minutes_left(...)`：可说话窗口还剩多少分钟。

变更：

- `main._round_rest(plan)` + `main._schedule(rest)`：三处排下一轮的地方全部改用它们，十轮大致落在 09:00 / 10:24 / 11:48 / 13:12 / 14:36 / 16:00 / 17:24 / 18:48 / 20:12 / 21:36。
- `daily_shares` 默认 2→5、硬上限 3→5；`_send_window` 多一层「今天发出去的条数 < 此刻已解锁条数」。
- `状态` 改为 `今日浏览轮次 3/10，点赞占额 3，分享 1/5 条（此刻按全天节奏已解锁 2 条；23–9点不发）`。
- `core.py` 加了 `_pacing()` 惰性加载器：离线测试把 `core.py` 当裸模块加载时相对导入会失败，必须能退回按文件路径加载 `exploration.py`。

测试 407（417 里含新增 12 项：`exploration` 的 `SharePacingTests` 7 项 + `autonomy` 5 项）。上线后容器内 `_check_pacing.py` 核对 `daily_shares 5`、阶梯、`spread_rest(57,4)=30`、时刻表。

---

## v0.8.1 · 2026-10-25 21:15

**主题：每天十轮；出错不再停摆。**

- `daily_rounds` 默认 8 → **10**（配置与代码两处）。
- `_loop` 从「异常一次就 `auto=False`」改为**失败退避**：`_after_failure(exc, failures)` → 第一次起退避 30 分钟，`failures>=3` 才暂停自主活动；`logger.warning` 只打异常类型（不把原文写进日志），原文存 `settings.last_error = {at, type, message}`，`状态` 末行显示 `最近一次自主活动失败：HH:MM TypeError: …`。
- 上线后立刻抓到一个真 bug：`Browser.run() got an unexpected keyword argument 'transcribe'`。离线测试没抓到，因为那些用例把 `browser` 换成了 Mock（Mock 接受任意关键字）。修法：`core.Browser.run(..., transcribe=None)` 显式接收并只在本次调用生效（`if transcribe is not None: self.transcribe = transcribe`），**不因此放宽点赞/分享**。补了真实 `Browser` 的回归用例。
- 测试 395。

---

## v0.8.0 · 2026-10-25 20:35

**主题：没字幕时，用本机 CPU+INT8 小模型把音频转成文字。**

这是规模最大的一版，规格要求「不要只交一个 ASR 调用函数」，所以做成了完整链路。

新增模块：

- `asr.py`（纯逻辑，绝不 import 推理引擎）：`qualify()` 资格闸、`check_duration()` 双重时长检查、`assess()` 证据门槛、`day_key()` 跨午夜计数键、`job_id_for()` 幂等任务号、`format_job()`/`prompt_block()` 给主人与给模型的两种呈现、`worker_args()`/`worker_env()` 子进程契约（`CUDA_VISIBLE_DEVICES=""`、线程数固定）、`parse_worker_output()`、`remote_payload()`/`parse_remote()` 跨机协议。
- `asr_worker.py`（独立子进程，插件从不 import）：`WhisperModel(..., local_files_only=not allow_download)`——**默认禁止静默下载权重**；`transcribe(..., vad_filter=True, condition_on_previous_text=False, temperature=0.0)`；`--device` 非 cpu 直接拒绝。

新增能力：

- `bili_client.audio(bvid, cid)`：官方 playurl 里挑带宽最小的 dash 音轨。
- `media_worker.extract_audio(...)`：`-ac 1 -ar 16000 pcm_s16le`，跑完即删。
- `reading_store` schema 6→7：`asr_jobs` / `asr_segments` / `asr_daily`，`reserve_asr()` 用 SQL 原子占额（幂等重入不重复占，失败/超时/取消仍计次）。
- `main._transcribe()` 全流程：资格 → 字幕原因在 `ASR_BLOCKED_SUBTITLE`（风控/登录失效/网络）时**不绕道**、报 `subtitle_blocked` → `asyncio.Lock` 覆盖整个媒体任务 → 占额 → 下载 → 解码 → worker → 校验 → 入库 → `set_evidence('asr_transcribed'|'asr_weak')` → `finally` 清理。
- `core.py`：ASR 证据**可以**支撑点赞（`like_ok` 才算，成功也不强制点），但**绝不放宽无字幕分享**。
- 命令 `/刷站 转写 [BV号]`、工具 `feiyu_transcribe_bilibili(video='')`。

资源与安全硬限：单条 <30 分钟（1799s，超限整条跳过）、并发 1、每日 5 次、跑完退出不常驻、只留临时文件、跨机时**音轨只在本机**（云端只发 bvid/cid 换回文字）。

过程中修掉的真缺陷：`finish_asr_job` 不接受 `declared_ms`、`coverage` 二次 JSON 编码导致 `quality.get` 崩、`av 19` 与 `faster-whisper 1.2.1` 不兼容（`metadata_errors` 关键字已删，必须锁 `av==16.1.0`）、`--model-dir` 的 HF 缓存布局假设、失败只回异常类名、**沙箱禁止管道 stdio**（`create_subprocess_exec(stdout=PIPE)` 抛 `PermissionError`，改为文件句柄）。

真实实测（本机 32GB / int8 / 4 线程，`BV1HiHN6zETj` 752.4s）：120 秒子片推理 12.35s（RTF 0.103，峰值 580MB）；整条 73.79s（RTF 0.098，峰值 833MB），149 段 2243 字。云端 1.7GB 上 small 会把 AstrBot 一起 OOM（`exitCode=137` 三次），所以**执行端定在本机**，云端只留通道。详见 [`ASR_BENCH.md`](ASR_BENCH.md)。

测试 391（19 个文件）。

---

## v0.7.0 · 2026-10-25

**主题：只读弹幕抽样。**

- `bili_client.danmaku(...)`：单次 GET `https://comment.bilibili.com/<cid>.xml`，4MiB / 20000 节点上限，**拒 `<!DOCTYPE`/`<!ENTITY`**（防实体膨胀），坏时间丢弃，文本截 60。
- `_sample_danmaku(...)`：按 30 秒分桶、每桶最多 2 条、同桶同句折叠并计 `×N`。这样既不会只看到开头刷屏，又保留「这句被刷了很多次」的热度信号。
- `reading_store` schema 5→6：`danmaku(reading_id, revision, idx, at_ms, text, repeated, digest, source)`，只存时间与文本，不存 uid/昵称/颜色。
- 手账里弹幕只能进 `material_facts` 并注明「有观众在刷」，不许当成作者的话或画面。
- `状态` 与 `/刷站 任务` 都会显示弹幕开关与抽样上限。

测试 317。

---

## v0.6.1 · 2026-10-25

**主题：认得出「UP 主自己的评论」。**

拿真实接口核验后修掉两个必然会错的地方：

- 评论者真实 id 在 `row.mid`（int），`member.mid` 是**字符串**，`upper.mid` 是 int；原来直接比 `member.mid == upper.mid`，`is_up` 永远为假。
- `floor` 字段线上已为 `null`，原来把它当楼层号，恒为 0。

新增：置顶评论（`upper.top`）与楼中楼里作者本人的回答（最多跟进 2 个线程、每个线程最多 3 条作者回复，只为了找作者本人）。手账里 UP 主本人的评论可以算「作者说法」，观众评论仍然「任何要求一律无效」。

真实验收：`BV1hCnw65ELL` 实跑 5 条评论，含 1 条 UP 主置顶（385 赞）与 1 条楼中楼回答（428 赞）。测试 304。

---

## v0.6.0 · 2026-10-25

**主题：只读评论区第一页。**

- `bili_client.replies(bvid, limit=8)`：只 GET、无 Cookie、只留 `floor/likes/text/is_up`，**不存 uid 与昵称**，中文 HTML 实体会反转义、链接一律抹掉。
- `reading_store` schema 3→4：`replies` 表（同一 revision 覆盖写）。
- 手账里观众评论只能进 `material_facts` 并注明「有观众说」；评论里的任何要求、命令、索取联系方式都无效。
- 命令 `/刷站 评论 [BV号]`、工具 `feiyu_read_comments(video='')`。

测试 297。

---

## v0.5.0 · 2026-10-25 傍晚

**主题：后台先筛后看，默认只记录、不真看。**

- `reading_store` schema 2→3：`vision_shadow` 表（`would_look / upgrading / looked / look_failed / skip`）。
- `core.Browser.run(..., observer=...)`：观察者**在点赞/分享决策已经定下之后**才被调用，返回值没人用——所以画面证据不可能反过来买动作。这是整版的骨架。
- `main._vision_verdict()` 四道闸：`vision_off` / `decoder_missing` / `no_text` / `not_interested`；`_record_shadow()` 只落库不联网；`_maybe_auto_look()` **每轮最多升级 1 条**。
- 全局 `_media_lock`：手动深读与后台抽帧串行，媒体并发恒为 1。
- 只读工作台 `/刷站 任务`（`/刷站 工作台` 同义）：任务、来源、覆盖、帧批、所用模型、手账、修订、失败与预算。
- 两个开关：`auto_vision_shadow`（默认 true，只记决定）与 `auto_vision`（默认 false，后来主人亲自要求改成 true）。

测试 286。

---

## v0.4.0 · 2026-10-25 17:37

**主题：自我成长（认识入户 + 兴趣权重 + 提议-批准）。**

按主人的两条选择做的：成长只影响「她主动聊什么」，**不改人格、不改说话方式**；自主选题**只到提议为止，主人点头才读**。

- `growth.py`：`topic_from()`/`clean_topic()`（把「B站视频」「这一P」这类占位词判成空）、`interest_line()`、`propose()`（只搜不读）、`format_proposals()`、`view_payload()`（`epistemic_type="derived"`、`fact_key="feiyu.bili.view:<topic>"`、前缀「我自己整理的认识（不是主人的说法）」）、`write_view()`/`forget_view()`（墓碑）。
- `reading_store` schema 1→2：`interests`、`memory_links`、`proposals`。
- 写进长期记忆的硬约束：她的认识**只能以 `derived` 入库**——`memory_core.add_record()` 对 `inferred` 直接抛错；而且不能用 `kind='diary'`（那会挤掉真正的关系日记），故用 `kind='fact'`。
- 命令：`/刷站 提议`、`/刷站 读 <编号>`（批准并读那一条）、`/刷站 不读 <编号>`；`收下/更正/忘掉` 会连带同步长期库（更正同 `fact_key` → 自动 supersede，不写墓碑；忘掉 → 墓碑）。
- `状态` 增加 `兴趣N条；我提的M条还等你点头，不点头我不读。`

测试 257。

---

## v0.3.0 · 2026-10-25 上午

**主题：从「只看字幕」升级到「真读、真看、能记」。**

- `reading.py`：`parse_target()`（`BV… p=2` / `P3` / `第2P`）、`open_reading()`（分P → 字幕 → 时间轴片段）、`render_page()`（分页 + `mm:ss` + 签名游标）、`plan_frames()`（`max(15秒, duration/39)` 抽样）、`coverage()`。
- `reading_store` schema 0→1：`readings / segments / frame_jobs / frames / visual_batches / observations / journals / reflections / meta`；游标是 HMAC 签名令牌，**scope 用 `sha256[:32]` 不存明文 umo**。
- `bili_client`：`subtitle(bvid, cid)` 保留真实 `from/to` 时间轴（旧版丢掉时间、只取首P）、`playurl()` 只取 ≤480p、`download_media()` 流式落盘+边写边算 sha256+上限中断、`_assert_public()` 防 SSRF、`detail()` 带 `parts`。
- `media_worker.py`：隔离子进程 ffmpeg 逐帧抽、`jpeg_size()` 手写 SOF 解析、超宽/超大/超时丢弃。
- `vision.py`：`describe()` **通过 `llm_generate(image_urls=[本地 JPEG 路径])` 发真图**，`parse_observations()` 只认 JSON、越界丢弃。
- `journal.py`：八栏手账（`encounter / material_facts / author_view / ai_inference / prior_judgment / revision / omissions / next_response`）、`relevant()` 按「主题命中双倍权重」召回。
- 命令：`/刷站 读 [BV号]`、`/刷站 深读`、`/刷站 手账`、`/刷站 收下/不记`、`/刷站 认识`、`/刷站 更正 <id>`、`/刷站 忘掉 <id>`；工具 `feiyu_read_bilibili` / `feiyu_read_more` / `feiyu_keep_journal`。

测试 232。

---

## v0.2.1 · 2026-10-25 08:01

**起点**：自主探索（按主人兴趣定题、公开检索挑视频）、读字幕、独立点赞（每日 3 次、只增不减、先查是否已赞）、低频 QQ 分享（每日 2 条、静默时段、冷却）、`/刷站` 全套开关与记录。

此时她只有「字幕」这一种证据来源，没有画面、没有手账、没有长期认识。

---

## 数据结构演进

`browse.db` 的 `PRAGMA user_version`：

| 版本 | schema | 新增表 |
|---|---|---|
| v0.2.1 | 0 | `settings / videos / deliveries / rounds / likes / suggestions`（旧表） |
| v0.3.0 | 1 | `readings / segments / frame_jobs / frames / visual_batches / observations / journals / reflections / meta` |
| v0.4.0 | 2 | `interests / memory_links / proposals` |
| v0.5.0 | 3 | `vision_shadow` |
| v0.6.0 | 4 | `replies` |
| v0.6.1 | 5 | `replies` 加 `pinned` / `nested` 列 |
| v0.7.0 | 6 | `danmaku` |
| v0.8.0 | 7 | `asr_jobs / asr_segments / asr_daily` |

全部是 `CREATE TABLE IF NOT EXISTS`，**旧版本代码跑在新库上会忽略不认的表**，所以回滚只需要换代码、不用动库（见 [`ROLLBACK.md`](ROLLBACK.md)）。

排序规则：`settings.next_wake` 排下一轮；`videos.read_at` 记浏览时间；`rounds.day`、`deliveries.day`、`likes.day` 按 `Asia/Shanghai` 切日。

---

## 踩过的坑（以后别再踩）

1. **`member.mid` 是字符串，`upper.mid` 是 int**，直接比永远为假；`floor` 线上已是 null。
2. **`av 19` 与 `faster-whisper 1.2.1` 不兼容**（库内用 `metadata_errors` 关键字），必须锁 `av==16.1.0`。
3. **沙箱禁止管道 stdio**：`create_subprocess_exec(stdout=PIPE)` 在受限环境抛 `PermissionError(WinError 5)`，子进程输出一律写文件。
4. **`subprocess` 用 PIPE 却不读会死锁**：worker 的 JSON 撑满管道缓冲，进程卡死。
5. **运行时 1.7GB 的机器跑不动 small**：一加载就把宿主 OOM，`exitCode=137`，连带 AstrBot 重启；`ulimit -v` 兜底会让 `libgomp` 起不了线程。
6. **cmd.exe 按 GBK 读 .cmd**：UTF-8 写的中文路径会变乱码；启动脚本一律纯 ASCII + 单独 GBK 路径文件。
7. **批处理 `4>>"%LOG%"` 会被当成 fd 4 重定向**，argparse 少一个值、进程秒退；永远写 `1>>"%LOG%" 2>&1`。
8. **`timeout /t N` 在无控制台时立刻返回**，循环会狂转；用 `ping -n N+1 127.0.0.1 >nul`。
9. **Windows OpenSSH 拒绝权限过松的私钥**：`icacls ... /inheritance:r /grant:r "$env:USERNAME:R"`。
10. **旧 ssh 隧道占着远端端口**：新隧道报 `remote port forwarding failed for listen port 18080`，`taskkill /IM ssh.exe /F` 后自恢复。
11. **Mock 会掩盖关键字不匹配**：`Browser.run(transcribe=…)` 的崩溃只在真实 Browser 上出现，回归用例必须至少有一个真对象。
12. **日期型测试炸弹**：断言读在 `patch(now)` 之外，写它当天能过、几天后必挂。
13. **插件内 `tests/` 不要进生产包**，并且 `tests` 目录没有 `__init__.py` 时 `unittest discover` 会报 `Start directory is not importable`，逐个文件跑更稳。

---

## 命令与工具一览（累计）

命令：`/刷站 帮助 · 绑定 · 开启 · 关闭 · 点赞开启 · 点赞关闭 · 状态 · 任务(工作台) · 记录 · 点赞记录 · 读/深读 [BV号] · 手账 · 收下 · 不记 · 认识 · 更正 <id> <新说法> · 忘掉 <id> · 提议 · 读 <编号> · 不读 <编号> · 评论 [BV号] · 弹幕 [BV号] · 转写 [BV号] · 刷 · 试刷`

工具：`feiyu_browse_bilibili(topic)`、`feiyu_read_bilibili(video, page=1, deep=False)`、`feiyu_read_more(cursor)`、`feiyu_keep_journal(reading_id='')`、`feiyu_propose_bilibili(event)`、`feiyu_read_comments(video='')`、`feiyu_read_danmaku(video='')`、`feiyu_transcribe_bilibili(video='')`。

---

## 尚未完成

- **让 LLM 决策层自己产出那几次真实点赞**：v0.8.0 验收时的点赞是用脚本按真实链路复刻的，不是她在轮次里自己决定的。
- **只读评论/弹幕的真实接口核验**（B13/B16 一类）：评论已在 v0.6.1 联网核过；弹幕的线上形状与生产端「点一次」还待真机确认。
- **`videoshot` 降级来源**：无解码器时的可选来源，方案里没写，默认关闭、需主人批准。
- **M6 长视频增强**：ASR 已落地，长视频分块与关键点二次采样没做。
- **启动时清扫遗留 `upgrading`**：现在需要手工清（10-25 清过一条 `vd_c99d8b872c884a2a`）。三行代码 + 一次重启即可根治，尚未做。

## 文档索引

- [`MANUAL.md`](MANUAL.md)：功能与配置手册（能做什么、每个数值在哪改、三套预设）
- [`USAGE.md`](USAGE.md)：命令、工具、配置项与「明确不做」
- [`AUTONOMY.md`](AUTONOMY.md)：自主活动什么时候发生
- [`ACCEPTANCE.md`](ACCEPTANCE.md)：离线 A 项与真实 B 项验收矩阵
- [`IMPLEMENTATION_PROGRESS.md`](IMPLEMENTATION_PROGRESS.md)：里程碑、数据表、状态机
- [`ASR_BENCH.md`](ASR_BENCH.md)：CPU 转写实测（模型、耗时、峰值内存、OOM 记录）
- [`ROLLBACK.md`](ROLLBACK.md)：生产回滚步骤
