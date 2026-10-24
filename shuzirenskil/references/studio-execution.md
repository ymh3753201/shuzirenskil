# 多模型完整执行指南（2026-10-18）

rc7：普通原生声音连续制作先读 `continuous-production.md`，spec 在首次确认前写 `review_mode=continuous_candidate`；该模式用 review-production 放行候选片制作，全片 review/deliver 的真实验收保留。以下逐段完整听审是 full_per_segment、广告及严格音轨的路径。


rc6：先读 `automatic-quality.md`。环境由 setup_environment 准备，doctor 实测；自动音频报告不替代真实听审。既定范围用 advance 持续推进，首段检查通过后不追加用户确认。新入口原生声音安全裁尾与严格音轨保护分别处理。

这是新 `studio.py` 的执行协议。旧项目继续使用 `workflow.py`，两种项目文件不能互相改名混用。
代码与本地模拟测试完成，不等于账号、生成质量或商业表现通过。每个真实新组合先做一段样片；本次开发没有创建付费任务。

## 1. 选择模型和声音

| profile | 新执行器 | 参考音色 | 参考视频续接 |
| --- | --- | --- | --- |
| `wan3-reference` | 创建、查询、下载 | 可选；WAV/MP3 1–15 秒、15 MB | 上一段已审视频尾部 2 秒 |
| `wan3-first-frame` | 创建、查询、下载 | 不支持；可走独立严格音轨 | 不支持，使用 cut |
| `minimax-h3-reference` | 创建、查询、下载 | 可选；2–15 秒、15 MB | 尾部 2 秒 |
| `minimax-h3-max-reference` | 创建、查询、下载 | 可选；2–15 秒、15 MB | 尾部 2 秒 |
| `seedance25-reference` | 创建、查询、下载 | 可选；2–30 秒、15 MB | 尾部 2 秒 |
| `veo31-reference` | 创建、查询、下载 | 无独立参考音频输入 | 当前采用 cut；不把其他产品的 extension 协议混入 |
| `91topgo-grok-reference` | 新入口与旧入口均保留 | 不支持 | cut |
| `miku-grok-image` | 使用原 `workflow.py` | 不支持 | 原有分段路线 |

- `model_generated`：模型自主配音，完全不传音频参考。素材库中未选的音频不读取、不上传。
- `reference_timbre`：传选定音频，只把它当声音参考，不承诺保留原台词或保证音色一致。
- `locked_audio`：整条主音轨按真实句子切分；生成画面、视觉验收、fal `sync-lipsync/v2` 口型处理、再次看听。产品空镜不做口型，仍播放主音轨。最终生成无损音轨 MOV、原主音轨 WAV，以及便于发布的 AAC MP4。

配置中的严格一致指的是**最终音轨样本保留**，人物、动作和嘴形仍须逐段看听检查。模型自主配音无法靠提示词、seed 或输入指纹保证每段声音相同。

## 2. 建立项目并确认

在 Skill 根目录运行，以下 `/path/` 均替换成实际绝对路径：

```bash
python3 scripts/production_plan.py plan --spec /path/spec.json --output /path/plan-preview.json
python3 scripts/studio.py prepare --spec /path/spec.json --config /path/config.json --project /path/new-project
python3 scripts/studio.py preflight --project /path/new-project --env-file /path/private.env
```

`spec.json` 从 `assets/production-spec-example.json` 复制，按本次方案修改。`config.json` 从 `assets/studio-config.example.json` 复制：替换真实 Workspace、分辨率/帧率即可。默认不需要 `rates`、`currency` 或 `max_cost`；没有报价仍可正常准备、授权、提交、续接、口型处理和交付。未知金额记录为 `null`，不能冒充零费用。不要主动询价或要求用户接受未知价格。

仅当用户主动要求金额上限时，可选填写单价和币种，并在授权中添加 `max_cost` 与 `currency`。此时才检查明确的金额限制；旧项目已记录的金额上限继续生效，不静默删除。普通新项目不得自行添加这些字段。

| 服务 | 默认域名 | 专用变量示例 |
| --- | --- | --- |
| Wan | `{WorkspaceId}.cn-beijing.maas.aliyuncs.com` | `DASHSCOPE_API_KEY` |
| MiniMax | `api.minimax.io` 或 `api.minimaxi.com` | `MINIMAX_API_KEY` |
| Seedance | `ark.cn-beijing.volces.com` | `ARK_API_KEY` |
| Veo | `generativelanguage.googleapis.com` | `GEMINI_API_KEY` |
| 91topgo | `router.91topgo.com` | `SHUZIRENSKIL_91TOPGO_API_KEY` |
| fal 口型 | `queue.fal.run` | `FAL_KEY` |

API Key 不写入 config、项目或报告。新用户优先使用系统凭据库，配置步骤见 `setup-and-troubleshooting.md`；旧 `.env` 在 POSIX 系统须为 600，Windows 须检查 NTFS ACL。91topgo 也保留专用钥匙串。测试模式仅允许本机模拟服务，不能用 `test_mode=true` 调真实接口。

预检只查本地素材、配置和密钥是否存在，**不证明模型账号权限、余额或实际质量**。不要把没有通用免费权限查询接口的服务编造成“账号已验证”。

prepare 产生不可随意修改的 `plan.json`、`config.json`、`contract.json` 和 `approval-template.json`。用户确认实际图片、完整方案、声音方式、上传目的地、首段试片及制作请求范围后，把真实授权写入独立文件：

```bash
python3 scripts/studio.py authorize --project /path/new-project --evidence /path/approved-production.json
```

审批模板不包含金额字段。次数与秒数按已确认方案登记，有效期记录在同一份制作授权中，不为这些内部字段另加确认轮次。审批模板默认全部未确认。开发或离线规划授权不等于真实素材上传/付费授权；已有明确用户授权可据实记录，不重复询问。

## 3. 素材传输

MiniMax、Seedance 普通图片/声音和 fal 媒体可用 Data URI；Veo 将图片编译为 `inlineData`。91topgo 本地图片压缩成至多 800000 字节的 JPEG Data URI。大文件可改用私有 HTTPS 地址。

Wan 的参考音频、参考视频需要可读取 URL。将对象存储生成的短期 GET 地址（可选 PUT 上传地址）保存在权限 600 的 `bindings.json`，键是对应本地文件的 SHA-256：

```json
{
  "实际文件的SHA256": {
    "url": "https://存储域名/短期签名下载地址",
    "put_url": "https://存储域名/短期签名上传地址",
    "expires_at": "实际的带时区到期时间"
  }
}
```

`put_url` 可省略，表示文件已上传。执行器仅在授权后传输，GET 读回后核对完整文件指纹；不设置公共访问、不猜造上传接口、不把签名 URL 写入普通请求日志。PUT 大于 64 MiB 需由存储工具预先上传，再仅提供 GET。

Seedance 真人素材按当前账号的官方人像准入流程建立资产；可绑定 `url=asset://asset-实际ID`，同时 `portrait_authorized=true`。这由用户已授权的资产记录证明，执行器不绕过人像准入；普通 Data URI 不等于真人素材获准。

## 4. 首段和后续片段

```bash
python3 scripts/studio.py submit --project /path/new-project --job segment_0001 --env-file /path/private.env --bindings /path/bindings.json
python3 scripts/studio.py poll --project /path/new-project --job segment_0001 --env-file /path/private.env
python3 scripts/studio.py inspect --project /path/new-project --job segment_0001
python3 scripts/studio.py review --project /path/new-project --job segment_0001 --evidence /path/completed-review.json
```

`inspect` 抽取真实首末帧、全程每半秒以及片尾加密帧，并产生全为 pending 的验收表。必须完整正常速度看听、检查全部取样与最后一帧。原生声音须记录实际台词与真实字幕时间，逐字对照确认稿。后续报告绑定首段视频指纹，检查声音和人物是否一致；广告另外检查空间、产品保真、动态场景与事实/行动引导。

严格音轨的首段先进入 `base_review_pending`（检查生成画面），通过后是 `lip_ready`。再运行同一 `submit` 调口型、`poll` 下载、`inspect/review` 看听。首段完整通过后才能提交后续片段。fal 请求带关闭服务端自动重试的头，不使用循环音频或自动拉伸。

`continuity_mode=continue` 必须顺序执行，上一段完整通过后取结尾 2 秒，同时重新发送原始人物参考作为身份基准。Wan 先执行 `context` 得到本地尾部文件及其指纹，再配置 URL：

```bash
python3 scripts/studio.py context --project /path/new-project --job segment_0002
```

模型的参考视频只能提高连续性，不能保证接缝天然完美；输出仍需时长、身份、动作和接缝验收。

## 5. 超时恢复与只重做必要片段

每次 POST 前写入请求用量和 `submitting`。超时、异常响应或进程中断保留 `submission_unknown/submitting`，禁止再次 POST。到服务商控制台核对合同对应的请求，再保存：

```json
{
  "contract_digest": "contract.json 中的 digest",
  "request_digest": "state.json 对应 attempt 的 request_digest",
  "provider_console_checked": true,
  "approved_by": "实际核对者",
  "task": {"id": "已查到的任务ID"}
}
```

fal 的 task 还需原任务的 `status_url` 与 `response_url`。运行 `recover --job ... --evidence ...` 只查询恢复；下载故障再次 `poll` 也是原任务下载，不重新下单。没有查到任务不能把未知状态改成 planned。

确需重做时，记录用户对重做范围的授权及新的累计请求额度，无需报价，再运行 `revise`，证据包含 `contract_digest、job_id、video_sha256、approved_by、reason`。保留所有旧视频与请求历史。分镜 cut 只重做所选段；若重做首段，后面已完成的视频需要按新基准重新验收，默认不重新收费生成。连续 continue 的下游依赖旧画面，需要连带重新规划并纳入已确认的累计请求范围。仍在运行或未知的任务不能直接重做。

## 6. 合成与交付

```bash
python3 scripts/studio.py assemble --project /path/new-project
python3 scripts/studio.py inspect --project /path/new-project --job final
python3 scripts/studio.py review --project /path/new-project --job final --evidence /path/final-review.json
python3 scripts/studio.py deliver --project /path/new-project
python3 scripts/studio.py status --project /path/new-project
```

全部片段通过后才合成。使用实际解码帧数建立时间轴；严格音轨按累计样本位置换算帧边界，避免分段舍入累计漂移；不会冻结、循环或加速语音凑时长。所有片段的实际尺寸/帧率必须符合确认值。

严格路线的 `final.lossless-audio.mov` 解码后每个 PCM 样本与主音轨比较；`master.wav` 原样保存；`final.mp4` 的 AAC 是有损发布版，不能说字节相同。字幕是外置 SRT，不自动烧入干净视频；原生声音使用逐段听审时间码，严格音轨使用全片已审时间码。

合成成功是 `awaiting_final_review`。全片、全部接缝、音色、嘴形、文字与广告事实均通过后才能 `deliver`。交付时再检查文件指纹。指标记录请求用量、重做原因、首段首次检查结果和验收耗时；实际账单与获客表现需外部数据，不从合成成功推断。

## 7. 本地声音生成与真实对齐（可选）

`speech_worker.py` 需要另行部署的 CosyVoice 或 WhisperX 环境、源码与权重；本次开发不下载大模型，不假装 macOS 已有 GPU 推理环境。可在已配置 Python 环境中运行同一脚本。工作器禁止联网，缺依赖明确报错。

```bash
python scripts/speech_worker.py tts --spec /path/tts-worker.json
python scripts/speech_worker.py align --spec /path/alignment-worker.json
```

TTS 配置字段：`repo、model_dir、script_file、output、voice_rights_confirmed=true、mode=reference、reference_audio、reference_transcript、cosyvoice_version=3`。支持该本地模型确实提供的 `mode=preset、speaker`。输出 WAV 仍是待试听候选，不能自动宣称音色通过。

对齐配置字段：`audio、script_file、model_dir、output、language=zh、device=cpu|cuda`。60 秒以上增加 `blocks=[{start,end,text}]`，来自实际音频的粗时间块，每块不超过 60 秒且完整覆盖稿件；不要用文字估时当作已经对齐。工作器以实际波形重新对齐字符，生成 `alignment_reviewed=false` 的候选。人工校正句子边界并试听后才改为 true。NLTK `punkt_tab` 和本地对齐模型必须事先部署，不会自动下载。

## 官方协议依据

核对日 2026-10-18：[Wan API](https://help.aliyun.com/zh/model-studio/wan3-video-generation-api-reference)、[MiniMax 创建](https://platform.minimax.io/docs/api-reference/video-generation-v2-create)、[MiniMax 查询](https://platform.minimax.io/docs/api-reference/video-generation-v2-query)、[Seedance 创建](https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh)、[Seedance 人像资产](https://docs.volcengine.com/docs/ark/seedance-portrait-asset-guide?lang=en)、[Veo](https://ai.google.dev/gemini-api/docs/veo)、[fal 口型](https://fal.ai/models/fal-ai/sync-lipsync/v2/api)、[fal 队列](https://fal.ai/docs/documentation/model-apis/inference/queue)、[CosyVoice 示例](https://github.com/QwenAudio/CosyVoice/blob/main/example.py)、[WhisperX 对齐实现](https://github.com/m-bain/whisperX/blob/main/whisperx/alignment.py)。
