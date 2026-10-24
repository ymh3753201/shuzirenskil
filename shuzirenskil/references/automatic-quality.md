# 自动制作、环境准备与质检（rc6）

## 用户授权与 AI 工作分开

原有两次确认保持不变。第二次确认实际图片并开始制作，即授权计划内全部片段、免费本地分析、安全裁尾、拼接和质检。首段/逐段审核是 AI 应执行的工作，不是新一轮用户审批；能力齐备且合格时立即继续，不得每段问“是否继续”。失败、范围变化或真实能力缺口才说明具体原因。不得取消首段关卡、增加付费次数、重新 POST 未知任务或伪造听审。

## 一次准备本地运行环境

在 Skill 根目录执行（Windows 用已确认可用的 `python` 或 `py -3`）：

```bash
python3 scripts/setup_environment.py
python3 scripts/run.py doctor --self-test --output /实际项目/环境检查.json
```

首次安装会联网下载固定版本的本地语音识别依赖、Silero VAD、small 多语言识别模型；缺少 FFmpeg/ffprobe 时下载对应系统工具。不上传私人素材、不调用视频/图片服务。已有 Python 3.12 时直接建立隔离环境；其他可运行安装器的 Python 版本通过固定版本 uv 准备独立 Python 3.12。无 Python 时由 AI 按系统官方安装方式先准备 Python 3.12。Windows ARM64 不宣称已支持该二进制组合，优先使用 Windows x64 环境或人工核对可用组件。

依赖保存在用户目录 `.cache/shuzirenskil/runtime-v1`，与作品、密钥分开。可以通过 `SHUZIRENSKIL_RUNTIME_HOME` 指定其他磁盘。后续用 `scripts/run.py` 调命令，无需用户手动激活环境或修改系统 PATH。仅迁移源码不能复制一个机器的虚拟环境到另一系统。

`doctor` 退出码 0 表示本地自动音频环境可用，2 表示仍有缺项；实际推理自检使用静音测试音，不能证明中文识别准确率或数字人效果。`agent_audio_input` / `agent_full_video_input` 始终由本次会话实际工具确认，不从软件安装推断。制作前报告尚缺哪些检查，不能收费生成之后才发现完全没有检查路径。

## 自动分析先做，不因“无法直接听”全部推给用户

新入口 `studio inspect` 会准备抽帧和独立音频报告。旧入口使用：

```bash
python3 scripts/run.py workflow --project-dir /实际项目 inspect-audio
```

任意已存在视频也可单独分析：

```bash
python3 scripts/run.py audio_qc --video /实际视频.mp4 --script-file /确认稿.txt --output /检查目录
```

报告包含音轨、解码、静音、停顿、原始识别文字、识别时间码、VAD 语音区间与稿件差异。VAD 判断哪里有人声，ASR 把实际声音识别成文字；两者都可能误判。转写不以确认稿作提示，保留机器原文，不用脚本覆盖。繁简体、专名、同音字的修正必须基于实际听审，单独保留修正说明。没有模型或识别失败时仍输出可完成的技术检查与缺项，不能标为全部通过。

`audio-qc.json` 的 `automatic_checks_completed` 仅表示自动步骤已运行；`perceptual_review` 仍为 pending。音色自然、一致性、口型、动态动作必须由能实际读入完整声音/视频的工具检查。抽帧与播放器打开不证明 AI 已看听。当前会话没有这些工具时，先完成静音/识别/抽帧等能力，再仅请用户补真正缺少的项目。

## 长视频自动推进

新入口：

```bash
python3 scripts/run.py studio advance --project /实际项目
```

它读取已有授权，提交/查询原任务，遇到首段或后续真实审核返回 `awaiting_agent_review`，并给出模板、音频报告和 `evidence_destination`。AI 完成真实观察后，保存已填写证据到该目标，再次 `advance`；文件按片段和当前视频指纹命名，旧视频证据不能复用。不要把 pending 模板直接改成全 pass。遇到 waiting_provider 稍后再调用，未知/失败任务停止，不重试付费请求。最终检查通过后自动 deliver。

旧入口：

```bash
python3 scripts/run.py workflow --project-dir /实际项目 advance
```

它继续已有阶段，生成音频检查并在必要审核处交回 AI；AI 按原协议完成 `review-pilot` / `review` 后再次运行。也可传 `--pilot-evidence /真实观察.json`；字幕仍使用实际已审核的 `--timing-srt`。这些是 AI 内部操作，不是要求用户填写 JSON 或每段再授权。

## 裁尾与连续参考

新项目使用 `studio-1.1-tail-qc` 合同。自由配音/参考音色口播在真实片段审核通过后，默认清理末尾超过正常收声范围的无效尾巴，并保留约 0.3 秒；以已审最后一句时间保护尾字。背景声尾巴仅在真实看听已经通过、台词识别逐字一致、末尾识别置信度足够且 VAD/识别/已审时间一致时裁剪。不同检测不一致则保留并指出缺项，不猜边界。内部停顿不裁、不加速、不凑时长。

有意的产品展示/结束动作须在审核里填写 `tail_action=preserve` 和 `tail_observations`。空镜与严格主音轨自动保留原时间；后者仍逐样本校验。历史 studio-1.0 合同保留原时间轴，不偷偷更改其验收结论。

每段 `tail_edit` 记录原片指纹、裁点、原因；合成向上对齐至原视频帧，不插帧。连续 continue 模式用同一有效终点的最后 2 秒作为下一段参考；不足 2 秒会停止说明，不静默换接口或片段。原始视频保留。裁后全片、接缝、末句与末帧仍须重新检查。

旧入口原静音裁尾保持有效。如需处理背景声尾巴，AI 在完整原片看听后准备 `stitch --tail-evidence-file`：对象按 shot_id 为键，每项记录 `video_sha256`、`reviewer=human_user|agent_audiovisual`、`full_playback=true`、真实 `transcript`、`cues=[{start_seconds,end_seconds,text}]`、`observations` 和 `tail_action=auto|preserve`。先运行 inspect-audio；同一检测模块只在证据吻合时提供可裁点。此文件是 AI 质检产物，不增加用户确认轮次。

## 打包边界

仓库和通用安装包携带完整源码、安装程序、固定依赖清单、模型来源版本和配置模板；不携带 API Key、私人作品或某台电脑的绝对配置。通用包默认联网安装，不能称为全平台离线包。

可用 `setup_environment.py --export-cache /新缓存目录` 导出当前系统 Python 3.12 的全部依赖 wheel 和识别模型。缓存有文件校验和、系统/CPU 标签；离线使用 `--offline-cache`，须已经安装匹配的 Python 3.12、FFmpeg/ffprobe。缓存不包含 Python 或 FFmpeg，不跨系统使用，不把它宣传为完整离线环境。大文件放发布附件或安装包目录，不进 Git 源码历史。

验收模板的 reviewer 初始为空，避免误导为“必须由用户审核”。执行者应按实际证据填写 agent_audiovisual、文字检查 agent_visual，或实际参与审核的 human_user；不能只改名称就放行。
