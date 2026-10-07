# shuzirenskil

公开分享版：仓库只包含 Skill 代码和空白配置示例。每位使用者需要自行准备图片与视频服务商账号，并把自己的接口地址和密钥放在本机私密配置中。不要把填好的 `.env` 上传到 GitHub、飞书或聊天窗口。

2026-10-06 更新：新正常制作使用 3.0 角色声音设计，出图后核对，首段与最终检查声音匹配；新增包含真实末帧的无文字检查证据。详细步骤见 `references/voice-casting-and-text-review.md`。旧 2.0 项目不自动改合同或重生成。


这是一个给普通用户使用的 Codex 数字人口播视频 Skill。它先分析主题、稿件、PPT、文档、人物图、音频、字幕或已有视频，再让用户确认完整方案和正式参考图，最后才可能调用付费视频接口。

## 安装要求

- Python 3.10 或更高版本
- FFmpeg 和 ffprobe
- 需要自动字幕时：`whisper-cli` 和本地 Whisper 模型
- 视频制作时：默认首选 91topgo，Key 放入 macOS 钥匙串服务 `shuzirenskil-91topgo-video` 或 `SHUZIRENSKIL_91TOPGO_API_KEY`；MikuAPI 仅显式降级，使用 `shuzirenskil-mikuapi-video` 或 `SHUZIRENSKIL_MIKUAPI_API_KEY`。旧的通用变量 `SHUZIRENSKIL_API_KEY` 不再被视频流程读取，避免把两家服务商的密钥混用。
- 参考图固定走第三方中转站：配置 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`，模型固定为 `gpt-image-2`

不要把 API Key 写进 Skill 目录、项目记录、日志或打包文件。macOS 本机推荐使用钥匙串；临时开发 Key 才放在项目根目录私密 `.env`。

## 本地 `.env` 配置

1. 把 `.env.example` 复制到具体制作项目目录，文件名改为 `.env`。
2. 按服务商填写 `SHUZIRENSKIL_91TOPGO_API_KEY` 或 `SHUZIRENSKIL_MIKUAPI_API_KEY`，以及图片用的 `OPENAI_API_KEY` 和 `OPENAI_BASE_URL`。也可以只配置对应的 macOS 钥匙串。
3. 在 macOS 或 Linux 执行 `chmod 600 <项目目录>/.env`。脚本发现其他用户也能读取时会拒绝使用。

macOS 钥匙串保存示例（不要把真实 Key 写进文档）：`security add-generic-password -U -a "$USER" -s shuzirenskil-91topgo-video -w '<API_KEY>'`。

也可以用全局参数 `--env-file <私密文件路径>` 指定位置。如果该文件没有当前视频服务商的专用变量，脚本会使用该服务商的钥匙串；文件中的旧通用变量不会覆盖它。`check-provider` 只做免费连通性检查，不创建付费视频任务；`submit` 创建任务前还会免费核对凭据与目标模型，并要求已完成两次确认和免费预检。

本地生成的参考图不需要先上传到公共图床。`generate-image` 和 `bind-image` 默认使用 `--input-transport auto`：有公共 HTTPS 地址时直接使用，没有地址时自动把已确认的本地图片压缩成 JPEG Data URI，并在 91topgo 的 `reference_images` 中发送。只有明确选择 `--input-transport provider-file` 才会走旧的 Provider 文件上传流程。

91topgo 当前使用 `POST /v1/videos` 创建、`GET /v1/videos/{id}` 查询，时长字段为字符串 `seconds`。参考图使用 `reference_images`；本地生成图会压缩成不超过 800000 字节的 JPEG Data URI，直接放进视频请求，不依赖图片上传接口。已完成一次 1 秒纯文本链路验证，并复用带货视频 Skill 已验收的 91topgo 参考图合同。Provider 付费 POST 失败或状态不明时不会自动切换到 MikuAPI；需要在提交前显式选择 `--provider mikuapi`。

## 安全默认值

- 默认中文、9:16、720p、无字幕、无额外特效。
- 没有当前任务中的明确授权时，不调用真实视频接口。
- 一个计划片段最多提交一次。
- 新数字人口播必须提供每段与真实台词对应的表演节拍；声音身份保持一致，语调和情绪允许随句意自然变化。多段默认先生成第 1 段试片，正常速度看听并通过检查后才提交其余已授权片段。
- 超时或结果不明时停止，不自动重试。
- 91topgo 的生产合同保存已确认图片指纹和本地路径；JPEG Data URI 只在提交时生成，不写入合同、日志或项目文件。已有公共 HTTPS 图片仍可直接使用。
- 用户指定时长就先按目标设计文案并显式选择 `exact`；只给内容就选择 `content-fit` 并省略时长。15 秒只作单段上限，同一次请求不为了按次收费而补满时长。
- 新增免费 `plan` 命令：草稿阶段检查字数、自然估时、完整句子分段、请求秒数和次数；内容与目标不匹配时先修改 AI 草稿。未指定时长不再要求先填一个数字。口播提示词不能写固定秒数时间格；成片 `review` 统计长停顿，并单独检查正常速度下的口播节奏。
- Skill 已实现 `reference-to-video` 的 1-7 张职责参考图和最多 3 个预设声音。Files API 代码仍保留，但 2026-08-24 对 MikuAPI 的真实测试在第一张图片上传时返回 HTTP 404，因此该路线默认关闭。
- 用户自己的音频文件不上传。xAI 官方把该声音参考能力限制为可信合作方申请开放；没有权限和中转站验收证据时只用于内容分析与本地质检。默认声音策略为 `prompt_generated_voice`：使用 8 项结构化声音特征、同一 `voice_signature` 和同一张主参考图尽量保持一致，但不承诺音色完全一致。
- 2026-08-24 已用一次 1 秒、480p、16:9 真实请求验证提示词人声路线：只提交一次且不重试，用户 MP3 没有发送；成片包含有效 AAC 人声和可见口型，本地 Whisper 准确识别出请求台词“你好”。这不等于复刻用户原音色。
- 只有 `delivery-manifest.json` 显示 `pass` 才算交付完成。

## 能力状态

- 本地分段、预检、模拟接口、拼接和质检：可通过免费测试验证。
- 图片：固定使用第三方 `gpt-image-2` 兼容中转站，不依赖内置 Image2；本项目此前的真实生成已通过。
- 91topgo Grok 中转站：除此前 1 秒纯文本和参考图合同验证外，2026-10-05 本项目已完成两次 15 秒、720p、9:16 的人物参考图口播请求，均成功下载 24fps 原片。这证明该组合可运行，不证明人物自然度、长句逐字准确或跨段声音已经达到交付标准；本次人物偏僵硬正是优化依据。
- 剪辑：生成片段在拼接前会自动裁掉过长的尾部静音，质检会再次阻止尾部静音超限的成片交付。
- 帧率和速度：拼接保留原片帧率，不自动把 24fps 补为 30fps，也不通过整体缓速凑精确时长。新生成视频的最终质检必须检查动作、手势时机、口型和声音自然度。
- 分段：`auto` 默认按内容规划，不根据 15 的倍数推断意图。`exact` 联合分配完整句子和请求秒数，20 秒可以是 `10+10`；内容模式优先最少生成次数，再按需取秒数。新方案记录规划版本，已有合同仍按旧版本复核。
- MikuAPI Grok 中转站：当前 Key 已确认可看到 `grok-imagine-video-1.5`；4 秒、480p、9:16 常规单图、1 秒提示词人声，以及 15 秒、480p、16:9 单段提示词人声均已分别创建、查询并下载成功。15 秒原片为 15.041667 秒；这不等于长句逐字准确、跨段连续性或 720p/1080p 已通过。
- xAI 官方原生能力：`reference-to-video` 最多 7 张参考图、最高 720p，并可选最多 3 个预设声音；图片可用 HTTPS、Base64 或 Files API `file_id`。自有音频文件声音参考不是普通开放能力。
- MikuAPI 新字段状态：多参考图和预设声音已完成离线实现但尚未付费验收；Files API 已真实返回 HTTP 404。三项配置均保持关闭，默认走单图加提示词生成人声。
- 长视频：主参考图血缘和本地拼接可验证；真实人物、口型、声音连续性仍需付费样片验证，不能承诺。
- 多段声音质检：必须实听每段和拼接点，以 `cross_segment_voice_consistency` 记录证据；Whisper 台词转写不能替代音色检查。

写稿、时长决策和免费规划见 `references/duration-planning.md`；参考图职责、提示词规则和上传方案见 `references/video-prompt-guide.md`。
