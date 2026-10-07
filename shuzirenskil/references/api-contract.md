# 视频 Provider 合同

## 91topgo 首选合同（文档 + 参考图链路复用验证）

- API 根地址：`https://router.91topgo.com`
- 创建：`POST /v1/videos`（文档同时列出 `/v1/videos/generations` 试用入口；默认使用 `/v1/videos`）
- 查询：`GET /v1/videos/{id}`
- 模型：`grok-imagine-video-1.5`
- 已确认请求字段：`model`、`prompt`、`seconds`；`seconds` 按服务商示例发送字符串
- 状态：`queued`、`in_progress`、`completed` 等异步状态
- 完成地址：服务商声明为公开只读 CDN；Skill 仍要求最终下载地址为 HTTPS
- 模式：`reference-to-video`；请求使用 `reference_images`，每项为 `{"url": "..."}`
- 参考图：1-7 张已确认生成素材；公共 HTTPS 直接透传，本地素材压缩成不超过 800000 字节的 JPEG Data URI 后透传
- 图片上传：不调用 `/v1/files`；本地 Data URI 只在提交进程内生成，不写入合同或日志
- 声音：通过提示词要求模型生成原生人声；不发送 `reference_audios`，不上传音频文件
- Key：环境变量 `SHUZIRENSKIL_91TOPGO_API_KEY` 或 macOS 钥匙串 `shuzirenskil-91topgo-video`；不读取通用旧变量
- 降级：MikuAPI 只在提交前通过 `--provider mikuapi` 显式选择；91topgo 的 POST 超时、网络不明或状态不明时禁止自动切换，避免重复付费

91topgo 的免费检查只访问一个不存在的健康探测任务路径，用于确认鉴权端点可达，不创建视频任务。此前已完成一次明确授权的 1 秒纯文本最小验证：创建任务成功，状态轮询到 `completed`，结果下载成功，本地解析得到 H.264/AAC。参考图字段和 Data URI 传输复用带货视频 Skill 的真实验收合同。2026-10-05，本 Skill 的一个 30 秒项目按确认方案提交了两次 15 秒、720p、9:16 的人物参考图口播请求；两段均下载为 15.041667 秒、24fps 的 H.264/AAC 原片。这是链路和时长的实测，不是自然度质量通过；人物表演偏僵硬，不能据此承诺中文长句、口型和跨段声音达到交付标准。

## MikuAPI Grok 视频接口合同

资料核对日期：2026-09-01。官方能力依据 xAI 最新文档；实际请求地址继续使用用户指定的 `https://mikuapi.org`，模型继续使用官方标准 ID `grok-imagine-video-1.5`。官方模型页将 `grok-imagine-video-1.5-preview` 列为别名；当前 MikuAPI 已验证的是标准 ID，所以只记录别名，不自动替换、降级或重试。

## 三层证据必须分开

1. **xAI 官方支持**：说明模型原生能做什么。
2. **本 Skill 已实现**：说明代码能组织、校验和脱敏哪些字段。
3. **MikuAPI 已验收**：说明作者历史测试账号真实成功过什么；不代表新用户账号已有权限。

只有第 3 层证据才能打开中转站生产开关。官方文档和离线模拟测试不能替代真实兼容性验收。

## 当前已启用的 MikuAPI 合同

- API 根地址：`https://mikuapi.org`
- 创建：`POST /v1/videos/generations`
- 查询：`GET /v1/videos/{request_id}`
- 模型：`grok-imagine-video-1.5`
- 官方别名：`grok-imagine-video-1.5-preview`（不用于当前 MikuAPI 请求）
- 模式：单图 `image-to-video`
- 已验证请求字段：`model`、`prompt`、`image`、`duration`、`aspect_ratio`、`resolution`
- 图片字段：`"image": {"url": "https://..."}`
- 当前 Skill 时长：1-15 秒整数；实际验证过 4 秒常规短片、1 秒提示词人声短片和 15 秒单段提示词人声短片
- 当前 Skill 分辨率：`480p` 或 `720p`；实际只验证过 480p
- Key：环境变量 `SHUZIRENSKIL_MIKUAPI_API_KEY` 或 macOS 钥匙串 `shuzirenskil-mikuapi-video`；与 91topgo 密钥隔离
- 自动重试：关闭

## 已实现但默认关闭的合同

### 多参考图

`reference-to-video` 使用：

```json
{
  "model": "grok-imagine-video-1.5",
  "prompt": "人物 <IMAGE_1> 穿着 <IMAGE_2> 的服装看向镜头自然口播。",
  "reference_images": [
    {"url": "https://example.com/person.png"},
    {"file_id": "file_example"}
  ],
  "duration": 8,
  "aspect_ratio": "9:16",
  "resolution": "720p"
}
```

规则：1-7 张、最高 720p、不能同时传 `image`，也不能和视频编辑模式混用。每张图可独立使用公共 HTTPS 或 `file_id`；本 Skill 不把 Base64 写进项目合同。

### 预设声音

在 `reference-to-video` 中可增加：

```json
{
  "reference_audios": [{"voice_id": "eve"}]
}
```

官方最多允许 3 个预设声音，提示词以 `<AUDIO_0>`、`<AUDIO_1>`、`<AUDIO_2>` 对应。数字人口播默认只用一个声音。用户自己的音频文件属于可信合作方申请能力；本 Skill 没有实现、没有权限证明，也没有 MikuAPI 验收，所以绝不上传。

### Files API 图片上传

- 路径：`POST /v1/files`
- 编码：multipart/form-data
- 字段顺序：`expires_after`、`purpose`、`file`
- 单文件安全上限：48MB
- 默认有效期：86400 秒（24 小时）
- 允许有效期：3600-2592000 秒（1 小时至 30 天）
- 返回的 `file_id` 只保存在权限 600 的私密输入和上传账本中

`upload-inputs` 只上传已经展示并由用户确认的图片，不创建付费视频任务，不上传用户音频。2026-08-24 的真实测试在第一张图片上传时，MikuAPI `POST /v1/files` 返回 HTTP 404；音频未上传，视频生成 POST 也未发生。因此当前生产配置保持关闭。

## 默认声音合同

当前默认使用 `prompt_generated_voice`，不在请求中放 `reference_audios`。每个项目使用同一份结构化 `voice_profile`，其 8 项方案值在各段保持一致，并在项目、一致性记录和每段干运行请求中保存同一 `voice_signature`。固定的是声音身份与整体风格，句间音高、语速和情绪允许随内容自然变化。该指纹只能防止我们自己误改提示词，不代表模型保证音色。台词写入提示词只代表生成要求，每条成片仍需真实听写验收。

## 响应兼容范围

创建成功时优先读取 `request_id`，同时兼容以下任务编号：

1. `task_id`、`request_id`、`id`
2. `data.task_id`、`data.request_id`、`data.id`
3. `video.task_id`、`video.request_id`、`video.id`

HTTP 成功但没有任务编号时，记录脱敏错误并停止；不得自动再次创建。

- 进行中：`queued`、`pending`、`submitted`、`processing`、`in_progress`、`running`
- 成功：`completed`、`complete`、`succeeded`、`success`、`done`
- 失败：`failed`、`failure`、`error`、`cancelled`、`canceled`、`expired`

成功查询响应兼容 `download_url`、`video_url`、`video.url`、顶层 `url`、`result` 或 `data` 包装。下载地址必须是 HTTPS；下载外部文件时不转发 MikuAPI 鉴权头。状态查询可以继续，创建请求超时、429、5xx 或结果不明时不得自动重试。

## 真实验证边界

2026-08-22 作者历史测试账号的 `GET /v1/models` 返回目标模型。同日一个已授权项目只提交一次 4 秒、480p、9:16 单图请求，成功取得任务编号、轮询完成并下载 MP4。成片实测 4.041667 秒、480×848、24fps，包含 H.264 画面和 AAC 双声道音频；三处抽帧人物稳定，未见文字或水印；本地 Whisper 得到对应中文短句。

2026-08-24 对用户确认的 16:9 数字人图片执行了一次单独授权的 1 秒、480p 提示词人声测试。仅提交一次创建请求，没有自动重试，也没有发送用户 MP3。下载结果为 1.041667 秒、848×480、24fps 的 H.264 视频，包含 AAC 48kHz 双声道音轨；抽帧可见稳定人物和说话口型，本地 Whisper 对约 0.52-0.85 秒的有效人声识别为请求台词“你好”。这证明当前中转站的单图加提示词生成人声路线可用，但不证明能复刻用户音色，也不代表长句、长视频或跨段声音一致性已经通过。

2026-08-31 对用户确认的 16:9 数字人图片执行了一次单独授权的 15 秒、480p 单图提示词人声测试。只提交一次且没有自动重试，Provider 原片为 15.041667 秒、848×480、24fps H.264/AAC；抽帧中人物、服装和场景稳定，无新增字幕、Logo 或水印。本地 Whisper 覆盖完整口播，和确认稿相似度为 0.7031，并把“读懂”识别为近音的“独懂”。自动清理 0.875 秒过长尾部静音后，交付版为 14.166667 秒、1280×720、30fps H.264/AAC。该证据证明 MikuAPI 单次 15 秒请求可用，不证明长句逐字准确、跨段连续性或用户原音色复刻。

仍未验证：720p/1080p、长视频连续性、`reference_images`、预设 `reference_audios` 或用户自己的音频参考。`/v1/files` 已确认在当前 MikuAPI 根地址返回 HTTP 404。此前其他中转站失败任务不重试，也不属于 MikuAPI 证据。

## 脱敏要求

干运行记录必须把 URL 或 `file_id` 改成摘要。真实 Provider 输入单独存入权限为 600 的 `provider-inputs.json`；Files 上传账本同样为 600。Authorization、API Key、图片 Base64、完整 Provider 错误正文不得进入合同、日志或报告。

## 官方资料

- [xAI Grok Imagine Video 1.5](https://docs.x.ai/developers/models/grok-imagine-video-1.5)
- [xAI Reference-to-Video](https://docs.x.ai/developers/model-capabilities/video/reference-to-video)
- [xAI Image-to-Video](https://docs.x.ai/developers/model-capabilities/video/image-to-video)
- [xAI Imagine Files 输入](https://docs.x.ai/developers/model-capabilities/imagine/files/inputs)
- [xAI Managing Files](https://docs.x.ai/developers/files/managing-files)
