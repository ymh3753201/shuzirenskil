# 数字人口播视频 Skill（shuzirenskil）

这是供 Codex 使用的数字人口播视频 Skill。它先分析素材和口播稿，给出完整制作方案；用户确认方案后才生成参考图；用户确认实际图片并说“开始制作”后，才进入可能收费的视频生成。多段视频先验第 1 段，完成后还要检查画面、声音和拼接。

## 安装

1. 安装 Codex、Python 3.10+、FFmpeg（须包含 ffprobe）。
2. 下载本仓库 ZIP 并解压，或运行 `git clone https://github.com/ymh3753201/shuzirenskil.git`。
3. 将仓库内的 `shuzirenskil` 文件夹复制到 `~/.codex/skills/shuzirenskil`。注意要复制整个文件夹，包含 `SKILL.md`、`scripts`、`references` 和 `assets`。
4. 重新打开 Codex，让它读取 `~/.codex/skills/shuzirenskil/SKILL.md` 并运行本地验证脚本。安装完成后再配置服务商；安装本身不需要真实 API Key。

详细能力、配置与限制见 [Skill 说明](shuzirenskil/README.md)。零基础用户可直接使用配套飞书教程中的四份提示词。

## 私密配置

复制 `shuzirenskil/.env.example` 到具体制作项目目录并命名为 `.env`，按自己的服务商填写。它只是空白示例，仓库没有作者的密钥。不要把填好内容的 `.env`、API Key、请求报文、真实视频或人物素材提交到仓库或发进公共聊天。

视频服务商配置中的请求地址是服务商公开接口路径，不包含账号凭据；使用前仍要用自己取得的官方文档核对。新模型不能只改模型名，必须核对请求字段、图片与声音输入、任务查询、费用和验收方式。

## 本地检查

在 `shuzirenskil` 文件夹运行：

```bash
python3 scripts/validate_skill.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

这些检查只验证本地文件和模拟接口，不会替你申请或验证真实服务商权限。真实连通性检查应与付费生成分开进行。
