# 多模型、声音意图与长视频规划（2026-10-18 完整开发版）

## 已实现的边界

规划与执行分开：`production_plan.py` 免费生成本地计划，`studio.py` 在实际授权后完成创建、查询、下载、验收、拼接与交付。旧 `workflow.py` 项目不能改名迁入。完整模型表、密钥配置、素材传输、恢复和命令见 [执行指南](studio-execution.md)。

Wan 3 参考/首帧、MiniMax H3/H3 Max、Seedance 2.5、Veo 3.1 参考模式、91topgo 的新适配器已实现；MikuAPI 使用旧入口。文档支持、代码完成、模拟测试、账号可用、实际声音/画面通过是不同状态。Kling、官方 Grok 和 Runway Characters 仍为候选，未编造接口或宣称支持其全部能力。

## 视频模型生成口播人声

默认可省略 `voice`。规划器采用 `model_generated`，只取选定参考图，素材表里其他录音不会读取、绑定或发送。
明确要求参考音色时，填写 `"voice": {"mode": "reference_timbre", "audio_asset": "voice"}`，并在 `assets.voice` 登记所选录音的 `path` 和 `kind: audio`。模型参考样本的音色朗读确认稿。Grok 当前网关只支持按提示生成人声；Seedance 2.0、MiniMax H3 的参考模式支持传入样本。若要原样保留录音，说明新口播无法满足该要求。

## 本地使用

以下命令在 Skill 目录内运行；占位文件需换成真实本地素材。

```bash
python3 scripts/production_plan.py models
python3 scripts/production_plan.py plan --spec /绝对路径/production-spec.json --output /绝对路径/production-plan.json
```

从 `assets/production-spec-example.json` 复制。素材的相对路径以 spec 所在目录为准；预览请求中仅有 `asset://SHA256` 占位符，无法直接提交。计划文件包含本地素材路径与台词，作为私有项目文档保管。

所有输出采用新文件或新目录。更改已选素材或计划后应重新规划。

### 分镜与广告

新项目 `duration_mode` 默认 `reference`：`target_seconds` 只是参考，最终按口播、旁白和镜头内容自然完成；不得修改已调好的声音、表情或动作来凑秒数。明确硬上限才选 `content-fit`，明确硬性精确时长才选 `exact`。无 `shots` 时，按完整句子和模型允许秒数计算最少请求；`duration_mode=exact` 必须能用合法秒数与自然语速完成，不能补长静音。

有 `shots` 时，每个镜头独立规划，使用 `content-fit`。`talking_head` 必须包含该镜头的台词；所有口播镜头合起来须与全文相同。`b_roll` 为动态产品或环境空镜，无台词且必须指定合法的 `duration_seconds`。原生配音路线的空镜声音丢弃并留静音。可选 `merge_shots=true` 合并相邻同类型镜头，首段保持独立，合并总秒数须在模型范围内；continue 保留独立边界。合并有完整分镜说明，不宣称一定降低实际计费。

广告仍需 `purpose` 与现有 3.1 `ad_contract`：受众、真实价值、事实依据、行动邀请、场景与产品锁定、原生文字白名单；参考图要有相应 product/scene 职责。新规划不放宽现有广告合同。

## 人物和声音连续性

人物、服装、场景、光照和机位写入 `character_bible`；每段保留参考图和声音资料的相同指纹。指纹证明输入未变，不能证明实际人物或音色一致。后续需逐段对照首段，正常速度看听接缝。

`cut` 是有意的分镜剪辑；Wan/MiniMax/Seedance 参考模式的 `continue` 取上一段验收后结尾 2 秒，并保留原人物参考图。严格顺序依赖，新的声音与画面仍须验收；不支持续接的组合明确拒绝，不用重复首图冒充。

## 首段报告迁移

使用 `assets/pilot-review-template.json`。每项含 `status`（`pass/fail/pending`）和至少 8 字 `observations`；初始均为 pending。填真实试片 SHA256、实际听写和独立 `text_review`，广告增加 `ad_review`。看到和听到后才能修改状态，不能机械填写通过。

旧纯文字报告不再放行后续付费。若项目已在 production_started 且旧首段报告需要补充，应保留旧报告作为历史证据，对已有首段重新验收；使用 `review-pilot` 的重新验收路径，无需重新生成片段。不要手改状态或伪造通过记录。
