# 回滚（v0.8.0 → v0.7.0 → v0.6.1 → v0.6.0 → v0.5.0 → v0.4.0 → v0.3.0 → v0.2.1）

原则：**先停插件，再动文件；库只加表不改旧表，所以回滚代码不需要回滚数据库。**

## 生产环境（云服务器）

路径（与 `使用与回滚.md` 一致）：

- 插件：`/opt/qqbot/astrbot/data/plugins/astrbot_plugin_feiyu_bili`
- 配置：`/opt/qqbot/astrbot/data/config/astrbot_plugin_feiyu_bili_config.json`
- 数据：`/opt/qqbot/astrbot/data/plugin_data/astrbot_plugin_feiyu_bili/browse.db`
- 凭证：同目录 `bili_credential.json`（回滚时**不要**动它）
- v0.2.1 备份：`/opt/qqbot/feiyu-bili-backups/20261025-101946-v02/rollback.py`
- **v0.4.0 上线前整包备份**：`/opt/qqbot/feiyu-bili-backups/20261025-173630-v030-pre/`（`plugin/`（v0.2.1 实况）、`config.json`、`plugin_data/`（含 `browse.db` 与凭证），880K，权限 700）

### 一条命令回到 v0.2.1（上线后若出问题）

```sh
BK=/opt/qqbot/feiyu-bili-backups/20261025-173630-v030-pre
cp -a /opt/qqbot/astrbot/data/plugins/astrbot_plugin_feiyu_bili \
      /opt/qqbot/feiyu-bili-backups/$(date +%Y%m%d-%H%M%S)-v040-quarantine
rm -rf /opt/qqbot/astrbot/data/plugins/astrbot_plugin_feiyu_bili
cp -a "$BK/plugin" /opt/qqbot/astrbot/data/plugins/astrbot_plugin_feiyu_bili
cp -a "$BK/config.json" /opt/qqbot/astrbot/data/config/astrbot_plugin_feiyu_bili_config.json
docker restart astrbot
```

（`browse.db` 不用动：新表都是 `CREATE TABLE IF NOT EXISTS`，旧版会忽略；只有确实损坏时才用 `$BK/plugin_data/browse.db` 覆盖。）

步骤：

1. 停 AstrBot（或先 `/刷站 关闭`，等当前轮次结束）。
2. 备份当前状态（含新代码与新库），例如：
   `cp -a /opt/qqbot/astrbot/data/plugins/astrbot_plugin_feiyu_bili /opt/qqbot/feiyu-bili-backups/$(date +%Y%m%d-%H%M%S)-v04`
3. 用备份目录里的旧版文件覆盖插件目录（`rollback.py` 是现有备份脚本，优先用它；不要动 `browse.db`）。
4. 配置：可保留（多出的 `vision_enabled`/`growth_memory_sync` 等键旧版不读，无副作用）；若要完全回到旧配置，用备份里的 `astrbot_plugin_feiyu_bili_config.json` 覆盖。
5. 重启 AstrBot，确认 `/刷站 状态` 正常、`metadata.yaml` 显示对应版本号。
6. 可选清理：删 `data/plugin_data/astrbot_plugin_feiyu_bili/media/`（抽帧临时文件，可安全删除）。

数据库说明：

- v0.3.0 新增表：`readings`/`segments`/`frame_jobs`/`frames`/`visual_batches`/`observations`/`journals`/`reflections`/`meta`。
- v0.4.0 再新增三表：`interests`/`memory_links`/`proposals`；v0.5.0 再新增一表 `vision_shadow`（+ `idx_shadow_scope`）；v0.6.0 再新增一表 `replies`（主键 reading_id+revision+idx）；v0.6.1 给 `replies` 加 `pinned`/`nested` 两列；v0.7.0 再新增一表 `danmaku`（主键 reading_id+revision+idx）。全部仍是 `CREATE TABLE IF NOT EXISTS`（列用 `ALTER TABLE ADD COLUMN` 增量补），旧表结构一字未改。
- `PRAGMA user_version` 被设为 `6`；旧版不读它，忽略即可。v0.6.1 回滚到 v0.6.0 时，`replies.pinned`/`nested` 仍在、旧版只是不查这两列，无害；v0.7.0 回滚到 v0.6.1 同理，`danmaku` 表留着、旧版不查。
- 因此**回滚代码时不需要回滚库**；若库本身损坏，再用备份里的 `browse.db` 覆盖（会丢新增期间的阅读/手账/兴趣，属于预期）。

## v0.5.0 M5 的额外注意

- 新增两把开关：`auto_vision_shadow`（默认 true，只记录筛选决定）与 `auto_vision`（v0.5.0 默认 false，**v0.6.0 起默认 true**，每轮最多升级 1 条）。
- 回滚代码时这两键留在配置里无害（旧版不读）；不想留痕就从配置里删掉。
- `vision_shadow` 表可保留（纯记录，不参与旧逻辑）；要清空直接 `DELETE FROM vision_shadow;`。
- 回滚前先把 `auto_vision` 置回 false，别让在途轮次和新进程交错。

## v0.4.0 自我成长的额外注意

- 回滚代码**不会**自动删除已经写进长期记忆库 `astrbot_plugin_feiyu_memory` 的派生认识。那些记录是 `epistemic_type=derived`、`fact_key=feiyu.bili.view:*`，可以：
  1. 在长期记忆里按 `fact_key` 前缀手工清理；或
  2. 回滚前先在 QQ 里用 `/刷站 忘掉 <认识id>` 逐条撤回（会写墓碑并断链）。
- 回滚后 `memory_links` 表里的映射将无人使用，保留无副作用。

## 本地工作区

- 源码：`05_B站浏览/plugin/astrbot_plugin_feiyu_bili`（v0.8.0）。
- 发布副本：`05_B站浏览/_publish/astrbot-plugin-feiyu-bili`（仍是 v0.2.1，可直接作为本地回滚源）。
- 回滚本地：用发布副本覆盖源码目录的 `main.py`/`core.py`/`bili_client.py`/`exploration.py`/`_conf_schema.json`/`metadata.yaml`，并删除 v0.3.0/v0.4.0/v0.5.0/v0.6.0/v0.6.1 新增的 `reading.py`/`reading_store.py`/`media_worker.py`/`vision.py`/`journal.py`/`growth.py` 与新增测试。

## 触发回滚的信号

- 启动时报导入错误或 `browse.db` 被锁死（旧版与新连接共存异常）。
- 自主浏览/点赞功能出现与阅读无关的回归。
- 主人认为抽帧行为不符合预期。
- 长期记忆里出现「主人说过」口吻的 B 站认识（说明 derived 标注被绕过），立刻回滚并查库。
- 打开 `auto_vision` 后发现后台抽帧频率或模型花费超出预期（先关 `auto_vision`，必要时回滚）。
- 评论区读取出现异常请求量（先关 `comments_enabled`，必要时回滚到 v0.6.0 以去掉楼中楼跟进）。


## v0.6.0 只读评论的额外注意

- 新增两键：`comments_enabled`（默认 true，只在主人明确要求时读）与 `comments_limit`（默认 8，硬上限 20）。
- 回滚代码时这两键留在配置里无害（旧版不读）。
- `replies` 表可保留（纯记录，不参与旧逻辑）；要清空直接 `DELETE FROM replies;`。
- 回滚**不会**撤销已经写进手账 `material_facts` 的评论内容；那些只是文字记录，她没有把评论当作者观点，也没有据此点赞/分享。

## v0.6.1 置顶 + 楼中楼作者回复的额外注意

- 没有新增配置键：行为挂在 `comments_enabled`/`comments_limit` 之下，关掉评论就是一次请求也不发（包括楼中楼跟进）。
- `replies.pinned`/`nested` 两列由 `_add_missing_columns()` 自动补；回滚到 v0.6.0 时旧代码只查 `is_up/likes/text`，多两列无影响。
- 回滚到 v0.6.1 之前的代码时，多出的一次楼中楼请求（`/x/v2/reply/reply`）自然消失，不需要额外清理。
- 若担心请求数，先把 `comments_limit` 调到 4 或把 `comments_enabled` 置 false，再回滚。

## v0.7.0 只读弹幕的额外注意

- 新增两键：`danmaku_enabled`（默认 true，只在主人明确要求时读）与 `danmaku_limit`（默认 40，硬上限 120）。
- 新增一表 `danmaku`（主键 `reading_id, revision, idx`），`user_version` 5 → 6。全部仍是 `CREATE TABLE IF NOT EXISTS`，回滚到 v0.6.1 时旧代码根本不查这张表，无害；要清空直接 `DELETE FROM danmaku;`。
- 弹幕只读一个静态 XML（`comment.bilibili.com/<cid>.xml`），不跟随重定向、不带 Cookie、不发 POST。回滚代码后这个请求自然消失，服务器侧没有任何残留状态。
- 回滚**不会**撤销已经写进手账 `material_facts` 的「有观众在刷」；那些只是文字记录，没有据此点赞/分享，也没有被当成作者观点或画面。
- 若担心请求数或不想被读到弹幕，先把 `danmaku_enabled` 置 false，再回滚。

## 触发回滚的信号（v0.7.0 追加）

- 弹幕返回体异常大或解析耗时明显（已设 4MiB/20000 节点上限，仍应观察）。
- 弹幕内容出现在手账的 `author_view` 或被当成画面描述 —— 说明提示词约束失效，应立刻回滚并复查 `journal.py`。

回滚后如需再上线，先看 `docs/IMPLEMENTATION_PROGRESS.md` 第 5 节的「未完成/需批准」。
## v0.8.0 本机按需 ASR 的额外注意

- 新增三表：`asr_jobs`/`asr_segments`/`asr_daily`，`user_version` 6 → 7。全部 `CREATE TABLE IF NOT EXISTS`，回滚到 v0.7.0 时旧代码不查这三张表，无害。
- 新增 13 个配置键（`asr_enabled`/`asr_background`/`asr_model`/`asr_compute_type`/`asr_language`/`asr_threads`/`asr_daily_limit`/`asr_max_seconds`/`asr_max_mb`/`asr_download_timeout_s`/`asr_decode_timeout_s`/`asr_worker_timeout_s`/`asr_model_dir`）。回滚后它们被旧版忽略；想彻底清掉就把这些键从配置里删掉。
- **回滚不会删掉模型权重**（权重是运行依赖，不在插件目录里，通常在人机共享的 HuggingFace 缓存或 `asr_model_dir`）。要回收磁盘请自己按路径删，删前先确认没有别的程序在用它。
- 回滚不会撤销已写入的转写片段（`asr_segments`）与手账里的「机器转写」记录；这些都是文字证据，没有音频残留。要清空：`DELETE FROM asr_segments; DELETE FROM asr_jobs; DELETE FROM asr_daily;`。
- 临时媒体在**每次任务结束时**就已删除（成功/失败/超时/取消都会删），启动时还会清理 `media/asr` 下的遗留目录；所以回滚时通常没有音频要清。若确实残留（例如进程被强杀），回滚后手动删 `<data_dir>/media/asr/` 整个目录即可，这个目录只属于本插件的 ASR 任务。
- 回滚前建议先 `asr_enabled=false`（等价于一条都不跑），再换代码，避免在途任务占掉当日额度。
- **每日 5 次的计数不会随回滚消失**：`asr_daily` 里当天的计数仍在。若回滚后当天又装回 v0.8.0，计数照旧生效（这是有意的：额度是真实资源，不该靠重启重置）。

## 触发回滚的信号（v0.8.0 追加）

- 转写把 QQ 聊天拖慢（同机 CPU 争用）：先 `asr_threads=1`，仍不行就 `asr_enabled=false`。
- 出现「没有音频却报转写成功」「同一任务一天花掉多次额度」「任务异常结束后 `media/asr` 还留着音频」——都属于账目或清理失效，应立刻关开关并回滚。
- 手账的 `author_view` 里出现机器转写原文，或被当成画面描述 —— 提示词约束失效，回滚并复查 `journal.py`。
- 任何一次真实点赞无法用「本次 ASR 证据 + 既有点赞规则」解释清楚 —— 立刻关 `asr_enabled` 并回滚点赞路径。

回滚后如需再上线，先看 `docs/IMPLEMENTATION_PROGRESS.md` 第 5 节的「未完成/需批准」。
