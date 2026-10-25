# 大肥鱼逛 B站 · AstrBot 插件

让机器人在 B站 自己逛：看首页推荐、沿相关推荐继续看、换个公开兴趣词搜索；
读字幕判断内容，**独立决定要不要点赞、要不要分享给你**。不按固定关键词轮播，
也不要求每天交一份推荐日报——没有值得分享的内容时，她可以什么都不发。

主人明确点名一条视频时，她会**真的去读那一 P**：读字幕、必要时抽帧看画面、
翻评论区第一页、看弹幕抽样，把这次阅读写成八栏手账，并从中提炼**可以修正的认识**。

这是为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 写的 Python 插件，
运行在现有 AstrBot 内，不需要额外的 DSH 进程。

当前版本 **v0.8.0**。

## 它做什么

- **自主探索**：每次活动由当前人格与公开兴趣出发，规划「首页推荐 / 相关推荐 / 公开主题搜索」。
- **真读那一 P**：按官方 `playurl` 取受限视频流（无 Cookie、≤480p、单条 ≤32MiB），
  用 ffmpeg 隔离子进程抽帧（单帧 ≤2MiB、每页 4 帧），把真实 JPEG 交给多模态模型看。
- **只有文字也算证据**：字幕按时间轴分段入库、翻页带签名游标；没字幕就如实说「未读正文」。
- **八栏手账**：遇见与处境 / 材料事实 / 作者观点 / AI 推测 / 先前判断 / 认识修正 / 遗漏 / 下次如何回应。
- **认识可以修**：`收下` 后成为一条认识，`更正` 取代旧条，`忘掉` 写墓碑并级联失效。
- **会记住自己在意什么**：读过的主题累积成兴趣权重（只影响她主动聊什么，不改人格、不改说话方式）。
- **想读清单，但要你点头**：`提议` 只列候选，一条都不自己读；`读 1` 才算批准。
- **评论与弹幕只读**：评论第一页 + 置顶 + 最多 3 条 UP 主本人楼中楼回答（作者的话单独标注）；
  弹幕按 30 秒分桶抽样、同句折叠成 `×N`。两者都只当「受众反应」，不当作者观点、不当画面。
- **没有字幕时可以转写**：主人明确要读而这一 P 没有字幕时，用 CPU + INT8 的
  faster-whisper（单条 <30 分钟、每日 5 次、并发 1，跑完即退出并删音频）。
  转写只是「机器听的字」，不当官方字幕、不当画面，只有内容足够时才可能作为点赞依据。
- **点赞与分享分开**：喜欢可以只点赞；真正想给你看时才通过 QQ 分享。转写证据**不放宽分享**。
- **低频唤醒，不是定时推送**：后台按规划器给出的 30–240 分钟间隔醒来；QQ 有免打扰时段与分享冷却。
- **后台先筛后看**：默认只记录「本来会看哪一条」；打开 `auto_vision` 后每轮最多真看 1 条。
- **去重与限额**：看过的视频、点赞尝试、分享投递、每日额度、转写次数都落 SQLite，重启不重复。
- **失败不冒充成功**：点赞或发送结果不明确时记 `uncertain` 并占额度，不自动重试写操作。

## 权限边界

| 能力 | 状态 |
|---|---|
| 搜索 / 首页推荐 / 相关推荐 / 视频详情 / 字幕 / 评论 / 弹幕 | 只读 GET |
| 视频流与音轨 | 只读 GET，官方 CDN 白名单 + SSRF 私网检查，不带 Cookie |
| 点赞 | 唯一写操作，固定 `POST /x/web-interface/archive/like`，只传 `like=1` |
| 评论 / 弹幕 / 投币 / 收藏 / B站私信 / 投稿 | **没有实现，也没有注册工具** |
| 取消赞 | **不支持** |

点赞默认关闭，必须在主人私聊里显式 `/刷站 点赞开启`。点赞会影响该 B站账号的公开互动与推荐偏好；
不想影响主号时，请用单独的账号登录。

## 依赖

- AstrBot `>= 4.29.0`，OneBot v11（QQ）适配器。
- Python 3.10+；插件只额外依赖 AstrBot 自带的 `aiohttp`。
- **[astrbot_plugin_feiyu_memory](https://github.com/Deepsleep-one/astrbot-plugin-feiyu-memory) 的已绑定主人身份**：
  本插件复用它的后端 `_identity()` / `get_owner()` 做授权，没有它或未绑定主人时会拒绝执行。
- 一个支持工具调用的聊天模型（跟随接收会话当前模型）；要看画面还需要一个**多模态**模型（`vision_provider_id`）。
- 抽帧需要 `ffmpeg` / `ffprobe`（也可在配置里写绝对路径）。
- 转写需要 `faster-whisper`（见 [`docs/ASR_BENCH.md`](docs/ASR_BENCH.md)）；没装时如实报「引擎未就绪」，不静默换模型或改用云 API。

## 安装

1. 把 `plugin/astrbot_plugin_feiyu_bili/` 整个目录放进 AstrBot 的 `data/plugins/`。
2. 重启 AstrBot（插件在启动时加载）。
3. 在 AstrBot 插件配置里按需修改兴趣词、限额和免打扰时间（见下表）。
4. 扫码登录 B站（可选，但读受限字幕、真实点赞需要）：
   ```bash
   # 在 AstrBot 容器/运行环境里执行
   python /AstrBot/data/plugins/astrbot_plugin_feiyu_bili/tools/bili_login.py --generate
   # 用官方 B站 App 扫输出的 URL 对应二维码；再执行
   python /AstrBot/data/plugins/astrbot_plugin_feiyu_bili/tools/bili_login.py --poll <qrcode_key>
   ```
   凭证只写入 `/AstrBot/data/plugin_data/astrbot_plugin_feiyu_bili/bili_credential.json`（权限 0600）。
   `tools/bili_login.py` 顶部的 `DESTINATION` 是固定路径；如果你的 AstrBot 数据目录不同，
   请改这一行，或在插件配置里用 `credential_file` 指向实际文件。
   该工具只请求固定的 B站官方 HTTPS 接口，禁止重定向，不输出凭证。

> 也可以不登录：匿名仍可搜索、读部分公开字幕、读弹幕；受限字幕会返回明确原因并被记为「未读正文」。

## 首次使用

在**已绑定的主人 QQ 私聊**里：

```text
/刷站 绑定
/刷站 试刷 历史策略游戏      # 只预览：不写记录、不点赞、不投递
/刷站 点赞开启              # 确认要让她操作账号点赞时再开
/刷站 开启                  # 开启后台自主活动
```

`绑定` 会强制关闭后台活动，避免把分享发到错误会话。

## 指令

| 指令 | 作用 |
|---|---|
| `/刷站 绑定` | 把当前主人私聊绑定为接收会话，并关闭自动活动 |
| `/刷站 开启` / `关闭` | 开关后台自主活动；关闭会取消在途任务 |
| `/刷站 点赞开启` / `点赞关闭` | 单独控制点赞；关闭会取消在途活动，不撤销已有点赞 |
| `/刷站 读 BV号 [p=N]` | 读那一 P 的字幕并分页返回（带翻页游标） |
| `/刷站 深读 BV号` | 读字幕 + 真抽帧看画面 |
| `/刷站 手账 [reading_id]` | 用这次的材料写八栏手账（待核对） |
| `/刷站 收下` / `不记` | 收下手账为「认识」/ 只留作废稿 |
| `/刷站 认识` | 列出当前有效的认识 |
| `/刷站 更正 <id> <新说法>` | 用新的认识取代旧的（旧的标记被取代） |
| `/刷站 忘掉 <id>` | 撤回一条认识（写墓碑，级联失效派生内容） |
| `/刷站 评论 [BV号]` | 只读评论区第一页 + 置顶 + UP 主楼中楼回答 |
| `/刷站 弹幕 [BV号]` | 只读这一 P 的弹幕抽样（带时间、无作者） |
| `/刷站 转写 [BV号]` | 这一 P 没有字幕时，用本机小模型把音频转成文字 |
| `/刷站 提议` / `想读` | 让她列几条想读的候选（一条都还没读） |
| `/刷站 读 <编号>` / `不读 <编号>` | 批准 / 驳回某条候选 |
| `/刷站 刷 [主题]` | 手动逛一轮，可分享 |
| `/刷站 试刷 [主题]` | 只预览，不写记录、不点赞、不发送 |
| `/刷站 状态` | 开关、额度、下次唤醒、解码器、评论/弹幕/转写开关、认识与候选数 |
| `/刷站 记录` | 最近读过的内容（标注是否读到字幕） |
| `/刷站 点赞记录` | 最近点赞尝试与结果 |
| `/刷站 任务` / `工作台` | 后台画面任务、筛选决定与今日用量 |
| `/刷站 帮助` | 指令说明 |

## 配置

基础：

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关；后台活动仍需 `/刷站 开启` |
| `topics` | 4 个公开兴趣 | 兴趣种子，不是固定轮播表 |
| `auto_like` | `false` | 新安装是否允许自主点赞；也可用指令控制 |
| `daily_likes` | `3` | 每日点赞占额（含结果不明确），硬上限 10 |
| `daily_shares` | `2` | 每日 QQ 分享上限，硬上限 3 |
| `daily_rounds` | `8` | 每日活动轮次，手动与自动共用，硬上限 16 |
| `videos_per_round` | `3` | 每次活动最多读几条候选，硬上限 5 |
| `share_cooldown_minutes` | `120` | 自主分享最短间隔，最少 30 |
| `quiet_start` / `quiet_end` | `23` / `9` | QQ 免打扰时段（UTC+8）；期间仍可浏览点赞，分享暂存 |
| `credential_file` | 空 | 可选 B站凭证 JSON 路径 |
| `provider_id` | 空 | 留空跟随接收会话的聊天模型 |
| `data_dir` | `data/plugin_data/astrbot_plugin_feiyu_bili` | 独立活动库目录 |

阅读与画面：

| 键 | 默认 | 说明 |
|---|---|---|
| `vision_enabled` | `true` | 是否允许抽帧看画面 |
| `deep_read_frames` | `12` | 显式深读抽多少帧（硬上限 40） |
| `auto_vision` | `true` | 后台每轮最多真看 1 条 |
| `auto_vision_shadow` | `true` | 只记下「本来会看哪一条」，不下载不调模型 |
| `auto_vision_frames` | `4` | 后台升级时抽几帧 |
| `media_max_mb` | `32` | 单条视频流上限 |
| `frame_max_kb` | `2048` | 单帧上限 |
| `media_timeout_s` | `180` | 每次解码子进程时限 |
| `frame_keep_hours` | `72` | 帧文件保留时长 |
| `ffmpeg_path` / `ffprobe_path` | 空 | 留空则用 PATH |
| `vision_provider_id` | 空 | 看图模型（必须真支持图像） |

评论、弹幕、成长、转写：

| 键 | 默认 | 说明 |
|---|---|---|
| `comments_enabled` / `comments_limit` | `true` / `8` | 只读评论第一页；硬上限 20 |
| `danmaku_enabled` / `danmaku_limit` | `true` / `40` | 只读弹幕抽样；硬上限 120 |
| `growth_memory_sync` | `true` | 收下认识时同步写长期记忆（派生标注） |
| `proposal_limit` | `3` | 一次最多列几条想读候选 |
| `asr_enabled` | `true` | 是否允许本机转写 |
| `asr_background` | `false` | 后台自主活动是否也能触发转写 |
| `asr_model` / `asr_compute_type` | `small` / `int8` | 只换显式配置，不静默换模型 |
| `asr_language` / `asr_threads` | `zh` / `2` | CPU 线程上限 8 |
| `asr_daily_limit` / `asr_max_seconds` | `5` / `1799` | 每日次数、单条时长上限 |
| `asr_endpoint` | 空 | 跨机执行端（如 `http://172.17.0.1:18081`）；留空则用本地 worker |
| `asr_endpoint_token` | 空 | 执行端口令（`X-Feiyu-Token`） |
| `asr_remote_timeout_s` / `asr_total_timeout_s` | `900` / `1200` | 远程请求时限 / 整任务总 deadline |

## 数据与隐私

- 活动库 `browse.db` 独立于长期记忆库，不把浏览/点赞写成主人的事实；
  只有主人 `收下` 的认识才会以**派生（derived）**身份写入长期记忆，并带来源与证据。
- Cookie 只交给 B站客户端；不进模型上下文，不写日志，不复制到别处。签名媒体地址不落库。
- 搜索规划器只收到角色公开兴趣行与少量公开视频元数据，不收私聊记忆、主人身份状态或凭证。
- 视频标题、简介、字幕、评论、弹幕、转写一律视为不可信参考资料；权限与配额由程序控制，
  模型输出中的额外链接或控制码会被拒绝或清洗，评论/弹幕里的任何指令都不执行。
- 单条模型判断格式异常时只跳过该条，不点赞、不分享；整轮网络/模型异常会关闭后台开关，需人工检查后重开。
- 跨机转写时，云端只发 `bvid/cid`，**音轨只在本机下载解码**，云端不接收任何音频字节。

## 测试

```bash
python -m unittest discover -s tests -v
python -m unittest discover -s plugin/astrbot_plugin_feiyu_bili/tests -v
```

共 **391 项**离线测试（19 个文件），覆盖权限拒绝、严格 JSON、去重与配额、夜间暂存、
发送失败不重试、点赞只增不重试、客户端域名/重定向/凭证隔离、分页游标签名、
抽帧参数与 JPEG 尺寸、手账八栏与召回、认识修正与墓碑、shadow 筛选、评论/弹幕只读边界、
每日转写额度与并发 1、总 deadline、远程执行端契约等。
`tests/test_bili_client.py` 需要 `aiohttp`，其余只依赖标准库；
测试不访问真实 B站、不写账号、不发送 QQ 消息、不真点赞。

## 文档

- [`docs/USAGE.md`](docs/USAGE.md)：命令、工具、配置项与不做清单
- [`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md)：离线/真实验收矩阵
- [`docs/IMPLEMENTATION_PROGRESS.md`](docs/IMPLEMENTATION_PROGRESS.md)：里程碑、数据表、状态机
- [`docs/ASR_BENCH.md`](docs/ASR_BENCH.md)：CPU 转写实测（模型、耗时、峰值内存、OOM 记录）
- [`docs/ROLLBACK.md`](docs/ROLLBACK.md)：生产回滚步骤

## 致谢与许可

- B站读取接口的字段与 WBI 处理参考了 [dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll)
  内置的 `bilibili-mcp`（MIT，Copyright (c) 2026 AGI_Ananas）；本项目是独立的 aiohttp 实现，
  未搬入其评论、私信、定时器或 DSH 运行时。
- 本项目以 MIT 许可发布，见 [LICENSE](LICENSE)。
