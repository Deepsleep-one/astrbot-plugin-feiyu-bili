# 本地 CPU+INT8 转写实测（2026-10-25）

这份记录只写**实际跑出来的数字**。没有跑通的项写明原因，不用估算值冒充。

## 1. 测试环境

| 项 | 值 |
|---|---|
| 机器 | 阿里云 ECS，与线上 AstrBot 同一台容器 |
| CPU | 2 vCPU（`nproc` = 2） |
| 内存 | 总 1.7GB（`MemTotal` 1778596 kB）；跑测时 `MemAvailable` 约 690–890MB |
| 容器 | `astrbot`（soulter/astrbot，Debian 13），数据卷 `/AstrBot/data` = 宿主 `/opt/qqbot/astrbot/data` |
| Python | 3.12.15（容器内） |
| ffmpeg / ffprobe | 7.1.5（镜像自带，`/usr/bin`） |
| GPU | 无，`ctranslate2.get_cuda_device_count() == 0` |

运行时与权重都装在数据卷里，**不动 AstrBot 自己的 venv**：

- 运行时：`/AstrBot/data/_asr/asr_env`（448MB）
- 权重：`/AstrBot/data/_asr/models/faster-whisper-{tiny,base,small}`

## 2. 装了什么

| 包 | 版本 | 备注 |
|---|---|---|
| faster-whisper | 1.2.1 | |
| ctranslate2 | 4.8.3 | CPU 推理 |
| av | **16.1.0** | 先装成 19.0.2，**必须降到 16.1.0**（见 §5 缺陷 1） |
| onnxruntime | 1.31.0 | VAD |
| tokenizers | 0.23.3 | |
| huggingface-hub | 2.3.0 | |
| numpy | 2.5.4 | |

权重体积（`du -sh`）：tiny **75MB**、base **145MB**、small **504MB**。

VAD 模型 `silero_vad_v6.onnx` 随 faster-whisper 包自带（`faster_whisper/assets/`），**不需要联网下载**。

## 3. 测试素材

- 视频 `BV1HiHN6zETj` 第 1 分P，时长 753 秒，官方 DASH 音轨（AAC）。
- 下载 6.1MB，ffmpeg 转 16k 单声道 PCM WAV：752.4 秒 / 24MB。
- 实测统一取**前 120 秒**做子片（控制变量），`compute_type=int8`、`threads=2`、`language=zh`。

## 4. 结果

| 模型 | 加载 | 推理（120 秒音频） | RTF（推理/音频） | 子进程峰值 RSS | 段数 | 文本样例（同一段足球解说） |
|---|---|---|---|---|---|---|
| tiny | 0.84s | 23.44s | 0.195 | 307MB | 26 | 「好了 欢歌为和记录一话声欢迎这场用场联织赛的第八罗阿翔的为拉在主场来赢传曼城队的比赛」 |
| base | 1.53s | 46.68s | 0.389 | 419MB | 29 | 「好了欢迎各位和记录一会儿说完这场英朝联赛的第八轮阿斯的维拉在主场来迎战漫程队的比赛转场这边交给卖进来发」 |
| small | 加载成功 | **未跑通** | — | 约 590MB | — | — |

- small 那次加载成功（内存峰值约 590MB），但当时 av 19 导致 `transcribe` 抛 `TypeError`，没拿到文本；随后重试时把 AstrBot 一起 OOM 打死，因此**没有 small 的耗时与质量数字**，不编。
- tiny / base 的文本都明显有错字（「英朝联赛」= 英超联赛、「阿斯的维拉」= 阿斯顿维拉、「漫程队」= 曼城队）。这是**噪声解说的低质量输入**，不代表安静口播的效果，但也不能当作可用质量。

## 5. 三个真实缺陷（已修）

1. **av 19 与 faster-whisper 1.2.1 不兼容**：库内部调用 `av.open(..., metadata_errors="ignore")`，该关键字在 av 19 已被移除 → `TypeError: open() got an unexpected keyword argument`。修：`pip install --upgrade --no-deps "av==16.1.0"`。
2. **权重目录约定**：把权重目录直接传给 `--model-dir` 时，faster-whisper 仍按 HF 缓存布局查找 → `model_missing`。修：worker 新增 `resolve_model()`，接受「`--model` 本身就是权重目录」「`model_dir/faster-whisper-<size>/`」「`model_dir` 里直接有 `model.bin`」三种形态。
3. **失败信息只有异常类名**：`load_failed` / `transcribe_failed` 只有 `RuntimeError` 四个字，无法定位。修：多带 `message`（截 300 字）。

## 6. 本机（32GB）实测 small —— 主执行端候选

本机：Windows 11 / 32GB 内存 / NVIDIA RTX 4060 Laptop 8G（**不用它**，`CUDA_VISIBLE_DEVICES=""` 强制 CPU）。
运行时：`05_B站浏览/_tools/asr_lib`（pip `--target` 装，faster-whisper 1.2.1 / ctranslate2 4.8.3 / av 16.1.0，**`ctranslate2.get_cuda_device_count()` 本机 = 1，但 worker 强制 `device=cpu` 且清空 `CUDA_VISIBLE_DEVICES`，实测未用 GPU**）。
权重：`05_B站浏览/_tools/models/faster-whisper-small`（model.bin 483MB，走 hf-mirror 下载）。
ffmpeg：`imageio-ffmpeg` 自带的 `ffmpeg-win-x86_64-v7.1.exe`（本机原本没有 ffmpeg）。

| 音频 | 时长 | 加载 | 推理 | RTF | 峰值 RSS | 段数 / 字数 |
|---|---|---|---|---|---|---|
| 前 2 分钟 | 120s | 0.95s | **12.35s**（4 线程） | **0.103** | 580MB | 18 段 / 365 字 |
| 整条 | 752.4s | 0.79s | **73.79s**（4 线程） | **0.098** | 833MB | 149 段 / 2243 字 |

同一段足球解说的输出（可读性明显高于 base）：

```
[   6.48-  11.08] 好了 欢迎各位和记录一会儿说完这场英朝联赛的第八轮
[  11.08-  15.48] 阿苏的维拉在主场来迎战曼城队的比赛
[  29.69-  34.29] 这些英朝唯一保持全胜的球队就只剩下曼城了
[  39.29-  66.29] 哎呦,这边外金前场的响段,杰克逊,停一下,哇哟,哇,开场6分钟不到,阿斯多维拉是率先破门…
```

错字仍在（「英朝联赛」= 英超、「阿苏的维拉」= 阿斯顿维拉），但整句意思连贯，足以判断「这条在讲什么、值不值得看」。**12.5 分钟的视频 74 秒转完**，在 900s worker 超时 / 1200s 总 deadline 之内。

## 7. 云端 1.7GB：内存是硬约束（重要）

`docker events` 记录到容器被宿主 OOM killer **杀掉三次**（`die` + `exitCode=137`）：

| 时间 | 当时在做什么 |
|---|---|
| 18:55:45 | `pip install` 装依赖 |
| 19:06:15 | 下载 small 权重 |
| 19:12:58 | 首次加载 small 模型并转写 |

AstrBot 每次都被自动重启，线上聊天中断几十秒。

原因是这台机器只有 **1.7GB**：AstrBot 自身占 550–840MB，small 加载后整体顶到 1.4GB 以上就崩。

**用 `ulimit -v` 兜底行不通**（实测）：限制虚拟地址空间后 `libgomp` 直接 `Thread creation failed: Resource temporarily unavailable`，连 tiny 都加载不起来。虚拟内存上限管不住这种「线程栈 + 映射文件」的模型，反而先掐死 OpenMP。

结论：**在这台 1.7GB 的机器上，只有 tiny 与 base 是能安全跑的档位；small 需要更多内存、或换到内存充裕的机器。**

## 8. 对选型的含义

| 选项 | 可行性 | 代价 |
|---|---|---|
| **本机跑 small（主人已选）** | 已验证：RTF 0.098、峰值 833MB、12.5 分钟视频 74 秒 | 需要一条「云端插件 → 本机转写」的通道，且本机要常开 |
| 云端跑 base（int8, 2线程） | 可跑，峰值约 420MB，RTF 0.39（10 分钟音频约 4 分钟） | 质量偏弱，专名/术语错得多 |
| 云端跑 tiny | 可跑，峰值约 310MB，RTF 0.195 | 质量更弱，基本只能看「在讲什么话题」 |
| 云端跑 small | **当前机器不行**（OOM 打死 AstrBot） | 需要把内存加到 4GB 以上（或换机型） |

通道设计原则（同时满足「首版只一个权威执行端」与「音频不跨机」）：
**音轨只在本机下载、解码、转写；云端插件只把 `bvid`/`cid` 这几行文本发过去，拿回结构化转写。** 云端不下载、不传递任何音频字节。

## 9. 复现方式（本机）

```powershell
# 运行时（装到工作区内，不污染系统 Python；本机沙箱内 pip 需要 full-access 才能建临时目录）
python -m pip install --target 05_B站浏览\_tools\asr_lib faster-whisper "av==16.1.0" imageio-ffmpeg aiohttp psutil

# 权重（HF 直连不通，走镜像）
$env:HF_ENDPOINT="https://hf-mirror.com"; $env:HF_HUB_DISABLE_XET="1"; $env:PYTHONPATH="05_B站浏览\_tools\asr_lib"
python -c "from huggingface_hub import snapshot_download as d; d('Systran/faster-whisper-small', local_dir=r'05_B站浏览\_tools\models\faster-whisper-small')"

# 一条真实视频的实测（取流→解码→转写→打印时间/RTF）
python -B 05_B站浏览\_tools\bench_local.py BV1HiHN6zETj 43019799288 small --seconds 120 --threads 4
```

## 10. 复现方式（云端，仅作对照）

```sh
# 运行时（只装一次）
docker exec astrbot pip install --no-cache-dir \
  --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
  --target /AstrBot/data/_asr/asr_env faster-whisper
docker exec astrbot pip install --no-cache-dir --upgrade --no-deps \
  --target /AstrBot/data/_asr/asr_env "av==16.1.0"

# 权重（HF 直连不通，走镜像）
HF_ENDPOINT=https://hf-mirror.com HF_HUB_DISABLE_XET=1 PYTHONPATH=/AstrBot/data/_asr/asr_env \
  python -c "from huggingface_hub import snapshot_download as d; d('Systran/faster-whisper-base', local_dir='/AstrBot/data/_asr/models/faster-whisper-base')"

# 单档实测（脚本在 05_B站浏览/_tools/bench_model.sh）
docker exec astrbot sh /AstrBot/data/_asr/bench/bench_model.sh base 0 120 2 999999999
```

插件侧对应配置：`asr_lib_dir=/AstrBot/data/_asr/asr_env`、`asr_model_dir=/AstrBot/data/_asr/models`、`asr_model=base`（或 `small`，在内存够的机器上）。

## 上线后的现状（2026-10-25）

- **执行端 = 本机**（32GB，small/int8/4 线程）；云端容器**不装运行时、不放权重**：
  `_asr/asr_env`（439MB）与 `_asr/models`（720MB）已删除，容器内 `import faster_whisper` → `ModuleNotFoundError`。
- 云端插件配置 `asr_endpoint=http://172.17.0.1:18081` + `asr_endpoint_token`；链路
  容器 → 宿主 `host_asr_relay.py`（systemd 常驻）→ 反向 SSH 隧道（只绑云端回环 `127.0.0.1:18080`）→ 本机 `asr_service.py`。
- 实测健康检查（容器内）：`{"ok": true, "engine": "faster-whisper", "model": "small", "busy": false}`；无口令 → `forbidden`。
- 真实往返（容器发起，`BV1HiHN6zETj`/752.4s）：本机下载音轨 → 解码 → small → **78.0 秒**返回 149 段 2243 字。
- 内存已回到安全线：`available 689MB`、`astrbot` RSS 315MB；删除模型后 `dmesg | grep -c "killed process" == 0`（不再 OOM）。
- 结论仍然成立：**这个 1.7GB 的机器不该跑 small**。它现在只做 HTTP 转发，模型推理在本机。
