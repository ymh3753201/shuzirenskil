# 连续候选制作与全片统一验收（rc7）

## 两次确认之后持续做完

普通原生声音口播的新方案默认声明：连续生成计划内各段，AI 检查后裁尾拼接，最后统一验收。无论两段、三段或更多段，都不再按段向用户请求继续、质检或拼接许可。先在第一次方案里说明检查能力与缺项；第二次确认真实参考图并开始制作已覆盖上述工作。不增加收费次数，不跳过检查，不把 pending 改成 pass。

这是一个明确写入方案指纹的模式，不自动改写历史授权：

- Grok 旧入口：`python scripts/run.py workflow --project-dir <新项目> prepare ... --review-mode continuous_candidate`。
- 多模型入口：spec 顶层增加 `"review_mode":"continuous_candidate"`，随后按原流程 prepare / authorize。
- 广告、严格主音轨、含空镜，以及用户明确要求逐段审片：使用 `full_per_segment`，保留原有关卡。不要为使用新模式降级声音或改变素材要求。

安装环境后先执行 `python scripts/run.py doctor --self-test`，要求 `continuous_production_ready=true`。AI 还必须能真正读取图片。缺能力在生成前解决；本地 Python、FFmpeg、Whisper、VAD、OCR 均由安装器准备，不能要求学员逐个手工配置。

## 每段的继续制作检查

1. 下载原片，不改写。音轨解码、实际 ASR 转写、时间码与 VAD 检查先运行。原始识别独立保存，不用确认稿提示识别或覆盖结果。
2. OCR 在本地检查每个解码帧，输出文字、置信度、时间、位置。与 FFprobe 的实际总帧数对照，未完成或少帧不能放行。OCR 未发现不等于保证没有字。
3. AI 实际查看 manifest 中的全部取样，包含首帧、末帧、片尾密集取样和 OCR 疑似文字对应的每个真实帧。填写真实 frame_index 列表，不能只看开头、结尾或几张代表帧。
4. 有疑似文字，先看原帧；确认真实文字就 fail。确属误识别才逐项填写 `ocr_findings_review=[{detection_index,verdict:"false_positive",observations}]`，观察需明确具体。不要用低置信度或“可能不明显”为真实文字开白名单。
5. 台词不符、无效置信度数据、缺时间码、画面有明确缺陷均不能放行。机器台词比较只做繁简字形、标点和大小写统一，不模糊匹配、改专名或改数字。识别原文对比一致但个别词置信度低时保留低置信度提示，留到全片统一听审，不将统计置信度直接当成真实漏词或再次要求逐段确认；裁尾仍保留原末尾置信度保护。语音自然度、跨段音色、动态口型等未实际看听项目仍 pending。
6. 通过仅记录 `production_checked`，表示允许继续制作，`business_accepted=false`。若后来发现任何明确缺陷，立即停止，不以“最后再看”为由继续付费。

旧入口由 AI 执行：

```bash
python scripts/run.py workflow --project-dir <项目> advance
# 返回待 AI 检查时，AI 打开全部检查图片，填写 production-review-template.json 的副本。
python scripts/run.py workflow --project-dir <项目> review-production --shot shot_001 --evidence-file <真实观察.json>
python scripts/run.py workflow --project-dir <项目> advance
```

旧入口首段通过后提交其余既定片段；各原片的继续制作检查全部通过后自动裁尾、拼接。没有第三段新增审批。`inspect-production` 可重新查看未完成项。

多模型入口使用 `studio advance --project <项目>`，每次返回 `evidence_destination`。AI 将真实继续制作证据写入该路径，再次 advance；也可 `studio review-production --project <项目> --job segment_0001 --evidence <真实观察.json>`。按原顺序持续执行，直到完整候选片生成或出现真正故障。

上述待 AI 检查、等待服务商状态均是执行循环，不是用户问题。不要结束回合说“请确认第二段合格后我再剪辑”。最多在全片完成后说明需要补充的主观验收。若当前会话具有真实音视频理解工具，就由 AI 完成最终真实验收并交付。

## 无字要求和最终结果

普通口播生成画面始终无字；台词、数字、手势、产品卖点、总结句只能通过声音和表演表达。提示词统一使用不可删除的无字规则，但不能保证生成模型每次服从，所以结果必须检查。

用户已要求无字时，发现文字必须保存失败位置和证据，不能反问是否保留、自动打码、自动遮挡、裁掉有效口播或偷偷重生成。若原授权没有重做次数，只说明具体失败与所需修复范围；这属于异常处理，不是逐段审批。

连续模式依据已校验的真实 ASR/VAD/时间码保守裁尾，原始报告仍留存。原片不变、内部停顿不剪、声音不加速。原先完整审核流程的裁尾保护保持不变。合成后按原完整视觉、声音、台词和接缝要求验收；没有完整视听能力就提供明确标注“待最终听审”的候选片，不能生成合格交付清单。

明确区分：成功生成候选片、机器检查完成、最终验收合格。任何工具都不能通过填写假听审证据实现全自动合格。
