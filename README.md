# 大肥鱼逛 B站 · AstrBot 插件

让机器人在 B站 自己逛：看首页推荐、沿相关推荐继续看、换个公开兴趣词搜索；
读字幕判断内容，**独立决定要不要点赞、要不要分享给你**。不按固定关键词轮播，
也不要求每天交一份推荐日报——没有值得分享的内容时，她可以什么都不发。

这是为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 写的 Python 插件，
运行在现有 AstrBot 内，不需要额外的 DSH 进程。

## 它做什么

- **自主探索**：每次活动由当前人格与公开兴趣出发，规划「首页推荐 / 相关推荐 / 公开主题搜索」。
- **读字幕再判断**：优先读首 P 字幕；没有字幕只记「标题简介，未读正文」，**不点赞、不主动推荐**。
- **点赞与分享分开**：喜欢可以只点赞；真正想给你看时才通过 QQ 分享。
- **自然聊天也能触发**：注册 `feiyu_browse_bilibili` 工具，主人私聊里说「去刷会儿 B站」即可。
- **低频唤醒，不是定时推送**：后台按规划器给出的 30–240 分钟间隔醒来；QQ 有免打扰时段与分享冷却。
- **去重与限额**：看过的视频、点赞尝试、分享投递、每日额度都落 SQLite，重启不重复。
- **失败不冒充成功**：点赞或发送结果不明确时记 `uncertain` 并占额度，不自动重试写操作。

## 权限边界

| 能力 | 状态 |
|---|---|
| 搜索 / 首页推荐 / 相关推荐 / 视频详情 / 字幕 | 只读 GET |
| 点赞 | 唯一写操作，固定 `POST /x/web-interface/archive/like`，只传 `like=1` |
| 评论 / 投币 / 收藏 / B站私信 / 投稿 | **没有实现，也没有注册工具** |
| 取消赞 | **不支持** |

点赞默认关闭，必须在主人私聊里显式 `/刷站 点赞开启`。点赞会影响该 B站账号的公开互动与推荐偏好；
不想影响主号时，请用单独的账号登录。

## 依赖

- AstrBot `>= 4.29.0`，OneBot v11（QQ）适配器。
- Python 3.10+；插件只额外依赖 AstrBot 自带的 `aiohttp`。
- **[astrbot_plugin_feiyu_memory](https://github.com/Deepsleep-one/astrbot-plugin-feiyu-memory) 的已绑定主人身份**：
  本插件复用它的后端 `_identity()` / `get_owner()` 做授权，没有它或未绑定主人时会拒绝执行。
- 一个支持工具调用的聊天模型（跟随接收会话当前模型）。

## 安装

1. 把 `plugin/astrbot_plugin_feiyu_bili/` 整个目录放进 AstrBot 的 `data/plugins/`。
2. 重启 AstrBot（插件在启动时加载）。
3. 在 AstrBot 插件配置里按需修改兴趣词、限额和免打扰时间（见下表）。
4. 扫码登录 B站（可选，但读受限字幕需要）：
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

> 也可以不登录：匿名仍可搜索、读部分公开字幕；受限字幕会返回明确原因并被记为「未读正文」。

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
| `/刷站 刷 [主题]` | 手动逛一轮，可分享 |
| `/刷站 试刷 [主题]` | 只预览，不写记录、不点赞、不发送 |
| `/刷站 状态` | 开关、额度、下次唤醒、凭证配置状态 |
| `/刷站 记录` | 最近读过的内容（标注是否读到字幕） |
| `/刷站 点赞记录` | 最近点赞尝试与结果 |
| `/刷站 帮助` | 指令说明 |

## 配置

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

## 数据与隐私

- 活动库 `browse.db` 独立于长期记忆库，不把浏览/点赞写成主人的事实。
- Cookie 只交给 B站客户端；不进模型上下文，不写日志，不复制到别处。
- 搜索规划器只收到角色公开兴趣行与少量公开视频元数据，不收私聊记忆、主人身份状态或凭证。
- 视频标题、简介、字幕一律视为不可信参考资料；权限与配额由程序控制，模型输出中的额外链接或控制码会被拒绝或清洗。
- 单条模型判断格式异常时只跳过该条，不点赞、不分享；整轮网络/模型异常会关闭后台开关，需人工检查后重开。

## 测试

```bash
python -m unittest discover -s tests -p "test_*.py"
python -m unittest discover -s plugin/astrbot_plugin_feiyu_bili/tests -p "test_*.py"
```

共 169 项离线测试（97 + 72），覆盖权限拒绝、严格 JSON、去重与配额、夜间暂存、发送失败不重试、
点赞只增不重试、客户端域名/重定向/凭证隔离、扫码登录等。客户端测试需要 `aiohttp`。
测试不访问真实 B站、不写账号、不发送 QQ 消息。

## 致谢与许可

- B站读取接口的字段与 WBI 处理参考了 [dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll)
  内置的 `bilibili-mcp`（MIT，Copyright (c) 2026 AGI_Ananas）；本项目是独立的 aiohttp 实现，
  未搬入其评论、私信、定时器或 DSH 运行时。
- 本项目以 MIT 许可发布，见 [LICENSE](LICENSE)。
