# 使用与运维

## 活动逻辑

每次后台唤醒先决定来源：

1. 首页推荐（`/x/web-interface/wbi/index/top/feed/rcmd`）
2. 沿真实浏览记录里的视频看相关推荐（`/x/web-interface/archive/related`）
3. 换一个公开兴趣主题搜索（默认按最新发布排序）

然后逐条读首 P 字幕，交给当前人格判断三件事：是否感兴趣、是否点赞、是否分享。
三者独立；没有字幕时点赞与分享都被程序强制关闭。

活动库记录：`videos`（看过）、`likes`（点赞尝试与结果）、`deliveries`（分享投递）、
`suggestions`（夜间暂存）、`rounds`（轮次与每日额度）。

## 时间与额度

- 后台每分钟检查一次；到点才真正发起活动。
- 一次活动后由规划器给出下次休息 30–240 分钟（带约 ±10% 浮动）。
- 每日活动轮次上限默认 8，手动与自动共用；分享额度用完**不会**停止浏览或点赞。
- QQ 免打扰时段默认 23:00–09:00（UTC+8）：期间仍可浏览、点赞，合适的分享最多暂存 5 条、24 小时。
- 主人刚在正常私聊说话后的约 5 分钟内，后台分享会避让。
- 分享冷却默认 120 分钟，每次活动最多分享 1 条。

这些是防刷屏、风控和模型费用的安全阀，不是要求每天必须完成的指标。

## 点赞行为

- 只增不减：调用前先 GET `has/like`；已赞则返回 `already_liked`，不再 POST。
- 唯一 POST 路径与固定字段：`bvid`、`like=1`、`csrf`（取自本地 `bili_jct`）。
- 网络超时或结果不明时记 `uncertain`，占当日额度，不自动重试。
- 点赞需要 `SESSDATA` + `bili_jct`；扫码登录后自动生效，无需重启。

## 扫码登录

`tools/bili_login.py` 只使用标准库，固定请求 B站官方 HTTPS 接口：

```bash
python tools/bili_login.py --generate
# {"url": "...", "qrcode_key": "..."}   ← 把 url 生成二维码给手机扫
python tools/bili_login.py --poll <qrcode_key>
# {"status": "waiting_scan" | "waiting_confirmation" | "success" | "expired"}
```

- 成功后凭证原子写入 `DESTINATION`（默认 `/AstrBot/data/plugin_data/astrbot_plugin_feiyu_bili/bili_credential.json`，权限 0600）。
- 目录不匹配时改脚本顶部 `DESTINATION`，或让插件配置 `credential_file` 指向实际文件。
- 插件在下一轮开始时按 mtime 自动重载凭证；不打印、不上传凭证。

## 故障与恢复

| 现象 | 处理 |
|---|---|
| 自动活动停止，日志出现「暂停」 | 网络/模型/接口异常会关闭后台开关；检查后用 `/刷站 开启` 恢复 |
| 单条内容没点赞也没分享 | 可能模型判断格式异常被跳过（该轮记 `partial`），属预期安全行为 |
| 一直拿不到字幕 | 未登录、字幕受限或风控；检查 `/刷站 状态` 的凭证配置，必要时重新扫码 |
| 想立即停止 | `/刷站 关闭`；只想停点赞用 `/刷站 点赞关闭` |
| 想确认是否真的点赞 | 用 B站 App 查看该视频，或看 `/刷站 点赞记录` 的 `liked` / `uncertain` |

## 升级

覆盖 `plugin/astrbot_plugin_feiyu_bili/` 后重启 AstrBot。活动库与凭证在
`data/plugin_data/astrbot_plugin_feiyu_bili/`，不在插件目录内，升级不会清空额度或去重记录。
