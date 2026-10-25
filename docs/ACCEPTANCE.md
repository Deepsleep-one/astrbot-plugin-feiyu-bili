# 验收矩阵（v0.8.0 离线）

两条线严格分开：**离线证据**（本机、无网络、无微信/QQ/真实模型调用）与**真实证据**（需要主人批准后才能产生）。当前所有「真实证据」格均为待做，不拿离线结果冒充。

## A. 离线已证明

### A1–A21：阅读与视觉（v0.3.0）

| # | 验收点 | 证据（测试位置） | 结果 |
| --- | --- | --- | --- |
| A1 | 只认合法 BV；13 位伪造串必须拒绝 | `test_reading.py::TargetTests` | 通过 |
| A2 | 同 BV+page 重读变成 revision+1，旧段清掉 | `test_reading.py::StoreTests` | 通过 |
| A3 | 分页每条字幕恰好出现一次，不重复不跳 | `test_reading.py::PagingTests`、`test_reading_plugin.py::test_read_pages_*` | 通过 |
| A4 | 游标篡改 / 换会话 / 换 revision 均被拒 | `test_reading.py`、`test_reading_plugin.py::test_read_more_rejects_*` | 通过 |
| A5 | 字幕真实时间轴保留，坏时间不入 segments | `test_bili_client.py::test_subtitle_keeps_real_timeline_and_drops_bad_times` | 通过 |
| A6 | 无字幕时只报 `metadata_only`，不编内容 | `test_reading_plugin.py::test_part_without_subtitles_*` | 通过 |
| A7 | 无解码器时 `visual_unavailable: decoder_missing`，不假装看过 | `test_reading_plugin.py::test_deep_read_without_decoder_*` | 通过 |
| A8 | 媒体下载：https/域名白名单/443/无私网/限 32MiB/失败清理 | `test_bili_client.py`（playurl/download/私网拒绝） | 通过 |
| A9 | 抽帧：无解码器→`decoder_missing`；源缺失→`source_missing`；超宽/超字节丢弃 | `test_media_worker.py` | 通过 |
| A10 | 真图入口：`image_urls` 逐张传本地 JPEG；空帧不发请求 | `test_vision.py::DescribeTests` | 通过 |
| A11 | 模型异常只报类型，不把内部错误或密钥写进结果 | `test_vision.py` | 通过 |
| A12 | 未解析的乱答不算「看过」，不落库 | `test_vision.py`、`test_reading_plugin.py::test_journal_is_not_written_*` | 通过 |
| A13 | 八栏手账字段完整、缺料进 omissions、去链接 | `test_journal.py::ParseTests` | 通过 |
| A14 | 收下→建认识；更正→旧条 superseded；忘掉→withdrawn | `test_reading_plugin.py::test_journal_keep_revise_and_forget` | 通过 |
| A15 | 不记的手账不进入召回 | `test_reading_plugin.py::test_discarding_a_journal_*` | 通过 |
| A16 | 相关召回标注「只能当回忆，不当作新证据」，且主题命中优先 | `test_journal.py::RecallTests`、`test_reading_plugin.py::test_selected_journal_*` | 通过 |
| A17 | 跨会话不可读、不可收下、不可写手账 | `test_reading_plugin.py::test_other_session_*` | 通过 |
| A18 | 非主人 / 群聊一律拒绝，且不产生副作用 | `test_reading_plugin.py::test_reading_is_refused_*`、`test_plugin.py` | 通过 |
| A19 | 阅读不推送 QQ、不占点赞/分享/轮次额度 | `test_reading_plugin.py::test_deep_read_without_decoder_*` 尾断言 | 通过 |
| A20 | 重启后库结构不变、已发游标仍有效 | `test_reading_plugin.py::test_reopening_the_store_*` | 通过 |
| A21 | 旧功能（自主逛/点赞/分享/记录/绑定）不回归 | `test_core` 38、`test_autonomy` 23、`test_plugin` 36 | 通过 |

### A22–A31：自我成长（v0.4.0）

| # | 验收点 | 证据（测试位置） | 结果 |
| --- | --- | --- | --- |
| A22 | 候选只来自她读过的主题；提议阶段**只搜索、不读** | `test_growth_plugin.py::test_proposals_come_from_interests_and_read_nothing` | 通过 |
| A23 | 批准哪条才读哪条；未批准的一条都不读 | `test_growth_plugin.py::test_approving_one_proposal_reads_only_that_video` | 通过 |
| A24 | 「不读」后该编号永不生效；未知编号不联网 | `test_growth_plugin.py::test_dismissed_proposal_is_never_read`、`test_unknown_index_*` | 通过 |
| A25 | 同一个视频不重复提议；已读不重复提议 | `test_growth.py::ProposeTests` | 通过 |
| A26 | 读一个分P即抬升她对那个主题的兴趣，权重可累加、跨会话隔离 | `test_growth.py::TopicTests`、`test_growth_plugin.py::test_reading_a_part_raises_*` | 通过 |
| A27 | 「收下」的手账才写成长期记忆，且必须是 `derived` + `kind=fact` + 前缀「不是主人的说法」+ evidence + op_key | `test_growth_plugin.py::test_kept_journal_writes_a_derived_view_*`、`test_growth.py::ViewTests` | 通过 |
| A28 | 更正同一主题复用同一 `fact_key`（长期库走 supersede），不多叠一条意见 | `test_growth_plugin.py::test_correction_supersedes_the_view_*` | 通过 |
| A29 | 忘掉认识时长期库那条一起写墓碑并断链 | `test_growth_plugin.py::test_forgetting_a_reflection_*` | 通过 |
| A30 | `growth_memory_sync=false` 时只留本地，不碰长期库 | `test_growth_plugin.py::test_sync_off_keeps_the_view_local` | 通过 |
| A31 | 兴趣/待点头数量在状态里可见，且提议不产生阅读 | `test_growth_plugin.py::test_status_reports_her_wants_*` | 通过 |

### A32–A42：M5 自主探索整合（v0.5.0）

| # | 验收点 | 证据（测试位置） | 结果 |
| --- | --- | --- | --- |
| A32 | 后台轮次先做筛选并把决定落库，全程**不下载、不调模型** | `test_m5.py::test_shadow_is_recorded_without_any_network_or_model_call`、`test_run_records_screening_and_never_downloads_when_off` | 通过 |
| A33 | 筛选闸门可解释：`vision_off`/`decoder_missing`/`no_text`/`not_interested` 四类跳过各有理由 | `test_m5.py::test_screening_gates_are_explicit` | 通过 |
| A34 | 观察者在点赞/分享决策**已定之后**触发，且返回值无人使用 → 画面不可能买动作 | `test_m5.py::test_observer_never_upgrades_like_eligibility` | 通过 |
| A35 | 观察者记录失败不影响轮次结果（`status=ok`） | `test_m5.py::test_broken_recorder_cannot_block_or_change_the_round` | 通过 |
| A36 | 自动视觉默认开（主人 2026-10-25 要求）；仍可显式关掉，关掉时只留 `would_look`，候选不被消费 | `test_m5.py::test_auto_look_is_on_by_default_and_takes_one_candidate`、`test_auto_look_can_still_be_switched_off`、`test_run_wakes_auto_look_only_for_settled_automatic_rounds` | 通过 |
| A37 | 无解码器时不消费候选（不把「没看成」当成看过） | `test_m5.py::test_auto_look_without_decoder_does_not_consume_the_candidate` | 通过 |
| A38 | 每次后台活动最多升级 1 条：`first_candidate` 只取最旧一条，升级后切到下一条 | `test_m5.py::test_first_candidate_is_oldest_would_look_only` | 通过 |
| A39 | 筛选决定与 scope 隔离，跨会话不可见；verdict 过滤与计数正确 | `test_m5.py::test_scope_isolation_and_verdict_filters` | 通过 |
| A40 | 手刷/试刷（非后台）不唤醒后台视觉；自动浏览关闭时也不唤醒 | `test_m5.py::test_run_wakes_auto_look_only_for_settled_automatic_rounds` | 通过 |
| A41 | 只读工作台：`/刷站 任务` 显示任务、帧批、模型、证据与筛选决定；`状态` 显示后台视觉开关与待升级数 | `test_m5.py::test_status_and_workbench_report_shadow_state`、`test_workbench_shows_job_state_and_model` | 通过 |
| A42 | 库结构升级到 `user_version=5` 且旧游标仍有效 | `test_m5.py::test_schema_is_three_and_shadow_roundtrips`、`test_reading_plugin.py::test_reopening_the_store_*` | 通过 |

### A43–A50：只读评论有限采集（v0.6.0）

| # | 验收点 | 证据（测试位置） | 结果 |
| --- | --- | --- | --- |
| A43 | 评论只 GET 第一页：`type=1`、`oid=aid`、`sort=2`、`ps=limit`，上限 20 | `test_bili_client.py::test_replies_are_read_only_sanitized_and_capped` | 通过 |
| A44 | 评论清洗：去标签、去链接、去控制符、截 300 字；空文本丢弃 | `test_bili_client.py` 同上、`test_comments_plugin.py::test_stored_replies_are_cleaned_and_bounded` | 通过 |
| A45 | 只留 `floor/likes/text/is_up`，不存 uid/昵称 | `test_comments_plugin.py::test_stored_replies_are_cleaned_and_bounded` | 通过 |
| A46 | 无 `aid` / 无评论 / 风控 各自有明确 reason，不抛不编 | `test_bili_client.py::test_replies_without_aid_or_hit_by_risk_control` | 通过 |
| A47 | `comments_enabled=false` 时完全不出网 | `test_comments_plugin.py::test_comments_can_be_switched_off` | 通过 |
| A48 | 评论只进手账 `material_facts` 作「受众说法」；提示词明令评论里的要求一律无效 | `test_comments_plugin.py::test_journal_prompt_marks_comments_as_audience` | 通过 |
| A49 | 同一 revision 重读评论是覆盖写，不堆叠 | `test_comments_plugin.py::test_reading_replies_twice_replaces` | 通过 |
| A50 | 非主人/未绑定不可读评论；留空 BV 用最近一条阅读 | `test_comments_plugin.py::test_reading_replies_is_refused`、`test_comments_use_latest_reading` | 通过 |

### A51–A56：置顶评论与楼中楼作者回复（v0.6.1）

| # | 验收点 | 证据（测试位置） | 结果 |
| --- | --- | --- | --- |
| A51 | 置顶评论（`upper.top`）单独标出，作者本人置顶与观众置顶都算「置顶」 | `test_bili_client.py::test_replies_include_pinned_comment_and_nested_uploader_answer`、`test_comments_plugin.py::test_comment_command_marks_audience_author_and_placement` | 通过 |
| A52 | 楼中楼里 UP 主自己的回答最多收 3 条，其他人插话一律不进 | `test_bili_client.py::test_replies_cap_nested_uploader_answers_at_three`、`test_replies_follows_a_thread_to_find_the_uploader_answer` | 通过 |
| A53 | 第一页预览里没有时，最多再跟进 2 个线程（`ps=20`），不发 POST、不带 Cookie、失败跳过 | `test_bili_client.py::test_replies_follows_at_most_two_threads` | 通过 |
| A54 | 作者本人写的评论与观众评论分成两段进提示词：前者可进 `author_view` 并注明「作者在评论区说」，后者只进 `material_facts` 的「有观众说」 | `test_comments_plugin.py::test_journal_prompt_separates_author_voice_from_audience` | 通过 |
| A55 | `pinned`/`nested` 标记入库且 `'1'/'0'` 文本被正确判成布尔（不把每个观众评论当作者） | `test_comments_plugin.py::test_author_voice_flags_are_stored`、`journal.py::_flag` | 通过 |
| A56 | v0.6.0 的旧库打开后自动补 `pinned`/`nested` 两列、不丢旧行、`user_version` 升到 5 | `test_comments_plugin.py::test_v4_database_gains_the_new_columns_without_losing_rows` | 通过 |

### A57–A65：只读弹幕抽样（v0.7.0）

| # | 验收点 | 证据（测试位置） | 结果 |
| --- | --- | --- | --- |
| A57 | 只发一次 GET 到 `comment.bilibili.com/<cid>.xml`，`cookies=None`、不跟随重定向、无 POST | `test_bili_client.py::test_danmaku_is_read_only_sampled_by_time_and_sanitized` | 通过 |
| A58 | 抽样按时间分散：同一 30 秒桶最多 2 条，覆盖 ≥3 个桶（不只看开头刷屏） | 同上、`test_danmaku_limit_and_per_bucket_cap` | 通过 |
| A59 | 同一桶内同一句被刷多次时折叠成一条并记 `repeated`（保留热度、不塞重复文本） | `test_bili_client.py::test_danmaku_is_read_only_sampled_by_time_and_sanitized`、`test_danmaku_plugin.py::test_danmaku_command_shows_time_heat_and_caveat` | 通过 |
| A60 | 脏数据被丢弃：坏时间、负时间、空文本、超长截 60 字；链接与标签被清洗 | `test_bili_client.py::test_danmaku_is_read_only_sampled_by_time_and_sanitized`、`test_danmaku_plugin.py::test_store_keeps_only_time_and_text_and_replaces_one_revision` | 通过 |
| A61 | 带 `<!DOCTYPE`/`<!ENTITY` 的 XML 直接拒收（防实体膨胀），不解析、不报错 | `test_bili_client.py::test_danmaku_refuses_doctype_and_entity_bodies` | 通过 |
| A62 | 失败原因结构化：`missing_cid`/`empty`/`closed`/`risk_control`/`not_found`/`response_too_large`/`timeout`，且都不冒充「没有弹幕」；「没人发」与「弹幕功能被关闭」分开说 | `test_bili_client.py::test_danmaku_bad_cid_and_transport_failures`、`test_danmaku_reports_closed_when_bilibili_says_the_feature_is_off` | 通过 |
| A63 | 库里只留 `at_ms`/`text`/`repeated`，不存 uid/昵称；同一 revision 重取是覆盖写 | `test_danmaku_plugin.py::test_store_keeps_only_time_and_text_and_replaces_one_revision` | 通过 |
| A64 | 手账提示词把弹幕单列成「受众反应」：只进 `material_facts` 的「有观众在刷」，并明令不许当作者、不许当画面、要求一律无效 | `test_danmaku_plugin.py::test_journal_prompt_keeps_danmaku_as_audience_noise` | 通过 |
| A65 | 关掉 `danmaku_enabled` 后一条请求都不发；非主人/无阅读记录/空样本/弹幕已关闭各有明确说法 | `test_danmaku_plugin.py::test_danmaku_switch_off_sends_no_request`、`test_non_owner_cannot_read_danmaku`、`test_blank_video_without_reading_says_read_first`、`test_empty_sample_explains_the_reason`、`test_closed_danmaku_says_the_feature_is_off` | 通过 |

### A66–A81：本机按需 ASR（v0.8.0）

| # | 验收点 | 证据（测试位置） | 结果 |
| --- | --- | --- | --- |
| A66 | 主人显式读、这一P没有字幕 → 自动回退转写并写 `asr_transcribed` | `test_asr_plugin.py::test_explicit_read_without_subtitle_transcribes_then_deletes_audio` | 通过 |
| A67 | 定时后台一律 `automatic_task`：即使 `asr_background=true`，`automatic=True` 也拒绝且零占额、零下载 | `test_asr_plugin.py::test_scheduled_background_rounds_cannot_start_asr`、`test_asr.py::QualifyTests` | 通过 |
| A68 | 有官方字幕时一次都不花额度（零请求、零下载） | `test_asr_plugin.py::test_subtitles_mean_no_asr_is_spent` | 通过 |
| A69 | 1799 秒放行；1800/3600 秒整条跳过（`too_long`）且提示明说「整条跳过」，不截前段冒充整条 | `test_asr.py::DurationTests`、`test_asr_plugin.py::test_thirty_minutes_or_longer_is_skipped_whole` | 通过 |
| A70 | 声明时长没超但解码后超限也终止（双重检查）；声明就超限时不下音频 | `test_asr_plugin.py::test_declared_too_long_never_downloads`、`test_asr.py::DurationTests` | 通过 |
| A71 | 每日 5 次硬上限：第 6 次 `budget_exhausted`；7 路并发只得 5 次；跨会话独立；跨午夜归零；重启后仍在 | `test_asr_plugin.py::test_sixth_request_is_refused_and_stays_at_five`、`test_concurrent_reservations_cap_at_five`、`test_reopen_keeps_todays_count_and_next_day_resets` | 通过 |
| A72 | 同一任务幂等重入不重复占额（`job_id_for` + `begin_asr_job`） | `test_asr_plugin.py::test_reentry_of_the_same_part_does_not_spend_twice` | 通过 |
| A73 | 并发固定为 1，且锁覆盖下载+解码+推理整段（不只是模型调用） | `main._asr_lock` 同时包住 `reserve_asr` 与 `_asr_run` 的全部 I/O | 通过 |
| A74 | 占额后失败/超时/取消仍计次（反复失败不能白吃资源） | `test_asr_plugin.py::test_worker_timeout_is_counted_and_cleaned_up`、`test_cancellation_marks_cancelled_and_cleans_up` | 通过 |
| A75 | 成功/失败/超时/取消都删除 `media/asr/<job_id>`；启动清理遗留目录且**不动目录外文件** | 同上 + `test_leftover_directories_are_removed_at_startup` | 通过 |
| A76 | worker 只接受受控参数：只传路径与标量、无 shell 拼接；环境清掉代理、`CUDA_VISIBLE_DEVICES=""`、线程固定 | `test_asr.py::WorkerContractTests` | 通过 |
| A77 | 引擎/权重缺失**不静默下载**：默认 `local_files_only`、报 `model_missing`；`--device` 非 cpu 直接拒 | `asr_worker.py` 契约（`ParseTests`/`FormatTests` 覆盖失败语义） | 通过（静态契约） |
| A78 | 弱证据不得点赞：静音/纯音乐/重复幻觉/乱码/过短/覆盖不足/段数不足 | `test_asr.py::AssessTests`、`test_asr_plugin.py::test_weak_transcript_is_labelled_and_cannot_be_used` | 通过 |
| A79 | 合格转写**可以**让点赞成立（仅当 `source='asr' and like_ok`），但从不强制点赞 | `test_asr_plugin.py` 的核心层用例（`client.like_calls==[BVID]`） | 通过 |
| A80 | ASR 不放宽分享：转写再合格，无字幕轮次 `share` 恒 false、`send_message` 一次都不调 | 同上（`sender.await_count==0`） | 通过 |
| A81 | 字幕被平台拒绝（`risk_control`/`login_required`/`network`…）时记 `subtitle_blocked`，**不绕道转写** | `test_asr_plugin.py::test_blocked_subtitle_does_not_route_around` | 通过 |
| A82 | 远程执行端只收 `bvid`/`cid`/模型与语言等标量，请求体里没有音频、没有路径、没有任何 URL | `test_asr.py::RemoteContractTests::test_payload_carries_no_audio_and_no_credentials` | 通过 |
| A83 | 配了远程端时插件**自己不下音频、不解码**（`client.audio` 与 `media.extract_audio` 一次都不调） | `test_asr_remote.py::test_no_local_audio_work_happens` | 通过 |
| A84 | 远程端的回答与本地 worker 走同一套归一化与证据门槛（`asr.parse_remote` → `parse_worker_output`/`assess`） | `test_asr.py::RemoteContractTests`（4 例）、`test_asr_remote.py::test_weak_remote_answer_cannot_support_a_like` | 通过 |
| A85 | 执行端连不上时报 `remote_missing` 并**不占额度**，绝不在本地偷偷加载第二个模型 | `test_asr_remote.py::test_dead_endpoint_is_reported_and_costs_nothing` | 通过 |
| A86 | 执行端正忙（HTTP 429）报 `remote_busy`，算一次已占额的失败 | `test_asr_remote.py::test_busy_endpoint_is_counted_as_a_failed_run` | 通过 |
| A87 | 执行端回的不是 JSON（网关错误页）时不得当成成功 | `test_asr_remote.py::test_garbage_answer_is_not_success` | 通过 |
| A88 | 远程回答的时长超过 1799 秒上限时整条拒绝（`decoded_too_long`） | `test_asr_remote.py::test_remote_answer_over_the_duration_cap_is_refused` | 通过 |

一次跑完（本机）：

```
$py = C:\Users\16pro\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe
python -B test_reading.py; test_vision.py; test_journal.py; test_media_worker.py; test_asr.py;
            test_bili_login.py; test_exploration.py        # 插件内 tests/
python -B test_core.py; test_autonomy.py; test_plugin.py; test_reading_plugin.py;
            test_growth.py; test_growth_plugin.py; test_m5.py; test_comments_plugin.py;
            test_danmaku_plugin.py        # 05_B站浏览/tests/
D:\DSH\QQBot\astrbot-tools\astrbot\Scripts\python.exe -B test_bili_client.py       # 需 aiohttp
```

结果：**391 项全部 OK**（2026-10-25，v0.8.0；含 4 个选档预算测试、5 个「模型不支持看图」测试、2 个 vision_provider_id 测试、19 个 M5 筛选/工作台测试、10 个评论测试、9 个弹幕测试、44 个 ASR 纯逻辑测试、21 个 ASR 插件级测试、7 个远程执行端测试与 48 个客户端契约测试）。

## B. 真实证据

2026-10-25 本机联网跑过一次（脚本 `_tools\live_accept.py`，产物 `_tools\live_accept.json`、`_tools\live\shown\frame_*.jpg`）；同日 17:37 生产部署后又在容器内跑了一次（`_tools\prod_e2e.py`，临时库、不碰生产 `browse.db`）：真实字幕 202 段、下载 28,045,001B、4 帧、`visual_observed`。

| # | 验收点 | 需要的动作 | 状态 |
| --- | --- | --- | --- |
| B1 | 真实 `playurl` 拿到 ≤480p 流并成功下载 | 联网单条视频 | **已证明**：`BV1HiHN6zETj`，4 个 ≤480p 档，32MiB 预算下自动选 360p，下载 28,045,001B |
| B2 | ffmpeg 真抽出 ≥1 张可解码 JPEG，尺寸 ≤854 | 已有 ffmpeg 7.1 | **已证明**：4 张 640×360 JPEG（24–34KB），时刻 94133/282400/470633/658900ms，人工看图确认是真实画面 |
| B3 | 多模态模型真的看到图并回结构化观察 | 云端容器实跑 | **已证明**：把 `vision.py` 与 4 张真实帧送进云端容器，`mimo-v2.6-flash` 返回 `state=visual_observed`、4/4 帧可见，读出比分 1-0→1-1→2-1 与时间码 06:45/30:38/55:28，`usage.image_tokens=880`。唯一未覆盖的是 AstrBot 的 `llm_generate` 传输层（本次用同一套 vision.py 走 HTTP），部署 v0.4.0 后即可顺手验证 |
| B4 | 手账由真实模型生成且不含编造画面 | 真实模型 | 待验证：生产已配好视觉 provider，在 QQ 里 `手账` 一条即可看全文 |
| B5 | 生产环境 `/刷站 状态` 显示解码器可用 | 部署后 | **已证明**：v0.4.0 上线后启动日志 `[feiyu-bili] 自主探索就绪 … decoder=True`，容器内 ffmpeg/ffprobe 均为 7.1.5 |
| B6 | 真实 QQ 私聊里一次完整「读→手账→收下」 | 生产 | 待主人一次 QQ 试发（代码、配置、解码器均已就位） |
| B7 | 生产长期库里能看到那条 `derived` 认识，且 `/feiyu` 上下文里带「〔派生整理〕」 | 部署后 + 记忆插件在跑 | 待批准 |
| B8 | 真实聊天中她主动提「我想读这几条」，主人回「读 1」才发起阅读 | 生产 + 真实模型 | 待批准 |
| B9 | 生产上跑几轮后 `/刷站 任务` 能看到真实筛选决定（谁本来会看、为什么跳过） | 部署 v0.5.0 + 开 shadow | 待批准（代码已就绪，云端仍 v0.4.0） |
| B10 | 主人同意后打开 `auto_vision`，实测「每轮最多升级 1 条」且点赞/分享仍按字幕证据 | 部署 v0.5.0 + 主人点头 | 待批准（代码已就绪；`auto_vision` 现已默认 true，部署即生效） |
| B11 | `replies()` 真实字段结构可用、清洗生效、不泄漏 uid | 联网只读核验（2026-10-25，无 Cookie） | **已证明**：`BV1HiHN6zETj` 读到 3 条（赞 9/589/260），清洗后无标签无链接；真实形状 = `upper.mid` 为 int、评论者 `row.mid` 为 int、`member.mid` 是**字符串**、`floor` 已为 null。据此修掉两处真缺陷（见下） |
| B12 | 置顶评论与楼中楼作者回复在真实数据里确实存在，且插件能读出来 | 联网只读扫描（`_tools/verify_replies.py`、`_tools/probe_nested.py`） | **已证明**：置顶——扫 8 个视频命中 2 个（`BV1HiHN6zETj`、`BV1uv411q7Mv`），另扫首页推荐 6 个视频有 4 个的置顶是 UP 主本人写的；楼中楼——`BV1hCnw65ELL` 实跑 `replies(limit=8)` 拿到 **5 条**，其中 `pinned=1`（UP 主置顶 385 赞）、`nested=1`（UP 主楼中楼回答 428 赞），`is_up` 命中 2 条，清洗全过；`probe_nested.py` 对 6 个视频跟进前 3 个线程（`ps=20`）在其中 1 个抓到 UP 主回复，其余 0（这些 UP 主在评论第一页附近确实没回复） |
| B13 | 只读弹幕 XML 的真实形状与可达性（无 Cookie `/comment.bilibili.com/<cid>.xml`） | 联网只读核验（`_tools/probe_danmaku.py`、`_tools/probe_danmaku_parts.py`） | **已证明**（2026-10-25）：5 个视频全部 `200`/`text/xml`/chunked，`p` 首段确为浮点秒且最大值 ≡ 时长，真实体无 DOCTYPE/ENTITY，插件 5/5 `reason=ok`；分P视频 `BV1Eb411u7Fw`（195P）P1/P2/P3 的 cid/chatid 各不相同（7200/600/976 条）→ 弹幕按分P取；不存在或已关闭的 cid 返回 200 + 0 个 `<d>` + `<text_side>本视频弹幕功能已被关闭</text_side>` |

| B14 | 真实 CPU+INT8 转写跑通一次（`faster-whisper small`），并记录耗时/峰值内存/线程数 | 本机装依赖 + 下权重 + 真跑 <30 分钟音频 | **已证明**（2026-10-25，本机 32GB，int8，4 线程，`BV1HiHN6zETj` 752.4s）：120 秒子片推理 12.35s（RTF 0.103，峰值 580MB）；整条推理 73.79s（RTF 0.098，峰值 833MB），149 段 2243 字，文本可读。云端 1.7GB 上 small 会把 AstrBot 一起 OOM（3 次 exitCode=137），base 峰值 419MB/RTF 0.389。详见 `docs/ASR_BENCH.md` |
| B15 | ASR 作为点赞依据在真实链路上成立（合格才点、弱证据不点、分享不变） | 生产部署 v0.8.0 + 主人指定的真实视频 | **未做**：离线门槛已由 A79/A80 证明，但**没有对任何真实视频点过赞**（真实点赞仍待主人点头） |
| B16 | 生产环境里 `/刷站 转写` 与 `/刷站 任务` 显示真实转写状态 | 部署 v0.8.0 | **未做**（云端仍是 v0.4.0） |
| B17 | 「云端插件 → 本机转写」通道真实可用：容器内发起、本机转写、只传 bvid/cid | 反向 SSH 隧道 + 本机 `asr_service.py` + 宿主 relay | **已证明**（2026-10-25）：容器内 `POST http://172.17.0.1:18081/transcribe`（`BV1HiHN6zETj`/cid 43019799288）→ 本机下载 6,168,363B 音轨、解码、small 转写，**78.0 秒往返**、149 段 2243 字；同一服务从宿主 `curl 127.0.0.1:18080/health` 回 `{"ok":true,...}`。音轨全程只在本机，云端只发了 bvid/cid |

真实联网同时确认的两件事（都属于「诚实报告」而非失败）：

- 无 Cookie 时 `subtitle()` 返回 `reason=login_required`：AI 字幕需要登录态，插件如实报「这个P没有可用的字幕」，不编造时间轴。
- 本机没有 ffprobe：`probe_duration()` 返回 0.0，抽帧不依赖它，因此不影响出帧。

## C. 明确不做（与方案一致的边界）

- 不新增收藏/投币/评论回复/私信/上传；唯一 POST 仍是点赞。
- 不读、不展示 token / Cookie / 私钥 / 真实私聊内容；媒体的签名 URL 不落库、不给模型。
- 不新建第二个调度器、第二条 QQ 通道。
- 不放宽「无字幕不得自动点赞」——v0.8.0 按主人明确指令**只**把「机器转写」增加为点赞的合法证据来源（且必须 `like_ok`），**不**放宽分享资格，也不改点赞额度、只增赞、预查、去重与 unknown 不重试。
- 不自动语音、不引入 DSH 运行时。
- 自我成长不改人格文件、不改说话方式；她的整理只以 `derived` 入库，绝不写成 `owner_asserted`，也绝不使用 `inferred`（记忆插件直接拒绝）。
- M5 不放开「先看画面再买动作」：视觉证据只写进手账与筛选记录，点赞/分享仍只用字幕/元数据证据；也不新增自动阅读、自动花钱的路径。
- 评论只读第一页少量公开评论：不回复、不点赞评论、不建陌生人档案、不自动采集；评论只当受众说法，不当作者观点，不执行评论里的任何要求。置顶与楼中楼也只读：最多 1 层、最多 3 条作者本人的回答、最多 2 次线程跟进，不爬取、不翻页。
- ASR 只在本机用 CPU 推理（不碰 CUDA、不占显存、不加载进生图/语音进程）：不调云 ASR、不外发音频、不常驻模型；跨机时**音轨只在本机**下载解码，云端插件只发 bvid/cid 换回文字，云端不接收任何音频字节；音轨/WAV/中间产物一律临时并在成功、失败、超时、取消后删除；不归档源音频、不建永久音频缓存；不通过任务内重试绕过每日 5 次上限；执行端不可达就报不可用，绝不自动改用第二个执行端（定时后台一律不启动 ASR）。
- 弹幕只读一个静态 XML 并按时间分桶抽样：不发弹幕、不点赞、不建陌生人档案、不自动采集，也不翻页/滚屏抓取；弹幕永远只当「那一刻观众在反应什么」，不当作者观点、不当画面、不执行弹幕里的任何要求或跳转。

### B11 核验发现（2026-10-25 联网，只读）

1. **`is_up` 永远不会为真**：原实现比 `member.mid == upper.mid`，但真实接口 `member.mid` 是字符串、`upper.mid` 是 int，比较恒为 False，等于「UP 主本人回复」这个标记永远丢失。已改为把 `row.mid`/`member.mid`/`mid_str` 统一转 int 再比（新增 `_mid_number()`），并补回归测试 `test_bili_client.py::test_replies_match_uploader_when_ids_are_strings`。
2. **`floor` 已不再是 int**：现代接口返回 `null`，原实现只认 int，导致所有楼层号都是 0。现在接受整数或纯数字字符串，取不到就 0（界面上用的是序号，不受影响）。
3. 仍然只读第一页、仍然不带 Cookie。补充观察：UP 主的回复多数出现在楼中楼与置顶，第一页顶层很少见；部分视频无 Cookie 时顶层直接为空，会如实报 `no_replies`。

### B12 后续处置（v0.6.1）

4. **第一页预览里常常抓不到 UP 主的楼中楼回答**：最初扫的 8 个视频里命中 0 —— 因为 `/x/v2/reply` 只带回每楼末尾几条预览，作者本人的回答常在更深的楼层。所以 v0.6.1 除了收置顶，还会对**最多 2 个** `rcount>0` 的楼层调 `/x/v2/reply/reply`（`ps=20`）找作者本人的话，最多收 3 条；仍只 GET、不带 Cookie、失败跳过、不翻页。
5. **跟进机制已在真实数据上验证**（`_tools/probe_nested.py`，2026-10-25，无 Cookie）：首页推荐 6 个视频、每个跟进前 3 个线程，`BV1hCnw65ELL` 抓到 1 条 UP 主楼中楼回答；插件整条路径 `replies(limit=8)` 在该视频返回 5 条（1 条置顶作者评论 385 赞、1 条楼中楼作者回答 428 赞），清洗全过、无 uid 泄漏。其余视频 0 命中是因为那些 UP 主在评论第一页附近确实没回复，不是漏读。
6. 这也意味着一次 `/刷站 评论` 在最坏情况下是 1（详情）+ 1（第一页）+ 2（线程）= 4 次只读请求，全部有界；关掉 `comments_enabled` 即一次都不发。

### B13 核验发现（2026-10-25 联网，只读）

1. **真实响应形态**：`HTTP 200`、`Content-Type: text/xml`、`Transfer-Encoding: chunked`（**没有 `Content-Length`**）。所以「声明长度超限就先拒」这条只是加分项，真正兜底的是流式累计上限；实测最大 692KB，离 4MiB 很远。
2. **`p` 字段是 10 段**：`time,mode,size,color,unixtime,pool,hash,rowid,weight,?`，第 1 段是浮点秒；5 个视频 `bad_time=0`，最大值分别等于各自时长。
3. **`<maxlimit>` 存在**：`BV1Eb411u7Fw` 的 P1 正好 7200 条，是被上限截断的典型 —— 所以 `total` 只能解释成「这份文件里解析出多少」，不能说成「这条视频有多少弹幕」。据此改了尾注措辞。
4. **空结果有两种，不该混为一谈**：不存在（或已关闭弹幕）的 cid 返回合法的空 `<i>` 加 `<text_side>本视频弹幕功能已被关闭</text_side>`。新增 `bili_client._danmaku_closed()` → `reason=closed`，`empty` 只表示「文件里没有可解析的弹幕」。
5. **按分P而不是按视频**：`BV1Eb411u7Fw`（195P）P1/P2/P3 的 cid 与 `chatid` 各不相同、条目 7200/600/976，插件用 `detail()` 里的分P cid 取对了位置。
6. **真实体里没有 DOCTYPE/ENTITY**（5+3 个全无），所以拒收规则不会误伤；它只在有人刻意构造时生效（有测试）。
7. **仍未证明的一项**：生产容器内的同一条路径。云端仍是 v0.4.0，不含弹幕代码；等批准部署 v0.7.0 后，一次 `/刷站 弹幕 <BV>` 即可闭环。