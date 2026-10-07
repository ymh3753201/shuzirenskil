# 执行流程

3.0 新项目增加两个必要步骤：绑定图片后，按 `voice-casting-and-text-review.md` 完成 `review-character`，再走原定 `authorize`；视频下载后先运行 `inspect-visuals`，真实完整检查后把 `text_review` 合入 `review-pilot` 或最终 `review` 证据。首段与最终还要检查 `voice_casting_match`。检查项只在实际看听后标记通过。


## 1. 方案前

清点材料，确定 `topic_only`、`new_video` 或 `postproduction_only`。已有视频默认不重新生成。已有音频默认只提供内容、语速和情绪参考；只有官方可信合作方权限与 MikuAPI 透传都确认后，才可以另行设计自有声音参考上传。

先按 `duration-planning.md` 确定用户目标、上限或内容推算。使用 `plan` 免费检查草稿；AI 自写稿在提交完整方案前自行改到合适的信息量。指定时长使用 `exact`，只有内容时用 `content-fit` 且不填时长。规划合适后再按实际分段设计表演并 `prepare`。

## 2. 第一次确认

完整文字方案已经出现在用户可见的主回复后，用户确认方案。方案必须锁定生成模式、参考图职责、输入传输方式、预设声音和视频提示词版本。运行 `confirm-plan` 记录方案摘要，但不要读取 API Key 或创建付费任务。

## 3. 第三方 gpt-image-2 和第二次确认

方案确认后，不再询问是否生图，直接运行 `generate-image` 调用第三方 `gpt-image-2`。该命令会裁成视频画幅并自动绑定主参考图。多段视频同时传入 `assets/continuity-template.json` 格式的一致性说明。

`image-to-video` 多段提示词人声默认只绑定一张主参考图，每段复用同一图片指纹和同一 HTTPS 地址。只有用户明确接受人物与音色漂移风险时，才可加 `--allow-derived-segment-images-with-risk` 绑定每段同源首帧。`reference-to-video` 绑定 1-7 张参考图，并按顺序为每张图声明唯一职责。实际查看所有会进入请求的图片，填写 `character-review-template.json` 中的观察和当前图片、声音指纹，运行 `review-character`。向用户展示图片及已选声音的简明说明，再等待原定第二次确认。用户明确确认图片并开始制作后，运行 `authorize`，并把该回复同时视为已接受方案中披露的上传、声音和动作风险；不要再发起第三次确认。

当前方案不得选择 `provider-file`：2026-08-24 的真实测试确认 MikuAPI `/v1/files` 返回 HTTP 404。用户自己的音频只用于内容和声音风格分析；默认把声音要求写入提示词，由模型生成口播人声。`upload-inputs` 代码保留用于未来兼容，但配置会直接停止。

## 4. 免费预检

运行 `preflight`。它会重新计算项目、图片和请求摘要，并写入脱敏请求及生产合同。检查：

- 脚本和分段未变化；
- 分段已经按完整句子重新计算并达到最少数量；句子数不能直接作为片段数；
- 每段 1-15 秒、完整句子结束；
- 每段秒数来自完整句子与用户时长意图；`exact` 请求总秒数等于用户目标，`content-fit` 按需请求且总秒数包含取整和余量，不超过用户上限；
- 图片指纹未变化；
- 模型、比例和清晰度未变化；
- 付费上限等于片段数；
- 请求中没有字幕字段和文字资产；
- 每个生产图都能追溯到确认素材，分镜预览图没有进入请求；
- `image-to-video` 只含 `image`，`reference-to-video` 只含 `reference_images`，两种模式没有混用；
- 多参考图数量、职责、顺序和提示词中的 `<IMAGE_n>` 映射一致；预设声音与 `<AUDIO_n>` 映射一致；
- 没有声音参考时，所有分段使用同一结构化 `voice_profile`、同一 `voice_signature` 和同一张主参考图，并且请求中不存在 `reference_audios`；
- 中转站尚未验收的多参考图、预设声音、Files API、续写和 1080p 开关保持关闭；
- 已向用户披露：作者历史测试账号曾看到目标模型；当前用户仍须用自己的账号免费检查。作者曾完成 4 秒、480p、9:16 常规单图、1 秒提示词人声和 15 秒、480p、16:9 单段提示词人声验证；15 秒原片为 15.041667 秒。长句逐字准确、原音色复刻、跨段连续性和 720p/1080p 仍未验证。

## 5. 提交和恢复

`submit` 先用项目快照指定的服务商及其专用凭据免费检查鉴权和目标模型，再把片段标为 `submitting` 并把尝试次数固定为 1。显式 `.env` 没有该服务商的专用变量时使用对应钥匙串，绝不读取通用旧变量。取得 `task_id` 后才标为 `submitted`。如果网络错误或响应缺少任务 ID，标为 `submission_unknown` 并停止。

多段新项目默认采用首段试片：第一次 `submit` 只发第 1 段，`poll` 下载后进入 `pilot_review_pending`。先运行 `inspect-visuals` 提取首段检查材料，正常速度完整看听首段，再补看全部取样和真实末帧。把当前视频的 `reviewed_clip_sha256`、`motion_naturalness`、`gesture_phrase_fit`、`lip_sync`、`voice_naturalness`、`speech_pacing`、`script_complete`、`identity_consistency`、`normal_speed_playback` 这些真实观察，加上 `voice_casting_match`、`no_generated_text`、结构化 `text_review`，以及逐字听写的 `spoken_words` 写入证据 JSON。`spoken_words` 去掉标点后必须与首段确认稿完全一致；听不清或不一致就停止，不能照抄确认稿冒充听写。运行 `review-pilot --evidence-file <JSON>` 后才可再次 `submit` 剩余片段；证据不足或试片文件变化也会阻止后续付费。首段失败时停止，不自动重试。第二次图片确认已经授权方案内的所有片段，无须再请求第三次确认。

`poll` 只处理已有 `task_id`。它可以重复查询和下载同一任务，但绝不创建新任务。Provider 返回失败状态时保留错误并停止。

## 6. 拼接和质检

每段先检测末尾连续静音；超过 0.6 秒时只裁掉尾部，保留约 0.3 秒自然收尾，不处理句子中间停顿。随后统一尺寸与音频格式，保留 Provider 原片帧率，再无交叉淡化拼接；不同源帧率先停下检查。默认不整体缓速凑时长。用 ffprobe 检查视频流、音频流、时长、帧率和分辨率；由 Codex 正常速度看听全片，再检查抽帧、人物一致性、画面文字、拼接点、动作、口型和语音完整性。

拼接结束后重新运行 `inspect-visuals`，传入每个拼接点的 `--boundary`；不能复用原片的无文字结论。实际检查后填写最终 `text_review`。`review` 不能只勾选通过，必须提供证据 JSON 和最终语音转写。新生成干净视频额外要求 `--motion-naturalness pass --gesture-phrase-fit pass --lip-sync pass --voice-naturalness pass --voice-casting-match pass --speech-pacing pass --normal-speed-playback pass`，每项都要有真实检查依据。多段视频还必须逐段听音高、音色、语速、情绪和口音，在证据中填写 `cross_segment_voice_consistency`，并使用 `--voice-consistency pass`。Whisper 只核对说了什么，不能证明是同一把声音。字幕时间轴不得明显超出成片结尾。脚本会保存证据、转写、抽帧和视频指纹；质检后视频或证据发生变化时，`finalize` 会停止。

字幕启用时，干净视频必须先通过。字幕只根据最终音频计时，并以已确认脚本为文字来源。

## 7. 交付

`finalize` 只接受所有必需检查为 `pass` 的项目。MikuAPI 当前完成了 4 秒常规单图、1 秒提示词人声和 15 秒单段提示词人声验证；最终报告必须保留本项目实际使用能力，并把 15 秒单段与长句逐字准确、跨段连续性等尚未验证能力分开。
