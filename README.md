# 数字人口播视频 Skill（shuzirenskil）

这是供 Codex 使用的数字人口播视频 Skill。它先分析素材和口播稿，给出完整制作方案；用户确认方案后才生成参考图；用户确认实际图片并说“开始制作”后，才进入可能收费的视频生成。多段视频先验第 1 段，完成后还要检查画面、声音和拼接。

## 安装

1. 安装 Codex、Python 3.10+、FFmpeg（须包含 ffprobe）。
2. 在需要使用 Skill 的当前项目目录打开 Codex。项目内安装位置是 `./.agents/skills/shuzirenskil`，其中必须包含 `SKILL.md`、`scripts`、`references` 和 `assets`。这样当前项目及其子目录可以使用它，不会覆盖用户全局 Skill。
3. 在 Codex 中请它使用内置 `skill-installer`，从本仓库的 `shuzirenskil` 路径安装，并把安装器的 `--dest` 指向当前目录的 `.agents/skills`。安装器支持 `--repo ymh3753201/shuzirenskil --path shuzirenskil --dest "$PWD/.agents/skills"`。没有内置安装器时，先下载本仓库 ZIP 并解压，再把整个 `shuzirenskil` 文件夹复制到该位置。GitHub 不可达时也可从作者提供的网盘包解压后复制。
4. 如果目标位置已有同名 Skill，先检查版本和差异，备份后再更新；安装器会拒绝覆盖已有目录。新开 Codex 会话后检查 Skill 是否可用，并运行本地验证脚本。安装本身不需要真实 API Key。

详细能力、配置与限制见 [Skill 说明](shuzirenskil/README.md)。零基础用户可直接使用配套飞书教程中的四份提示词。

## 私密配置

视频接口需要使用者自己的服务商配置。图片有三条路线：Codex 当前会话有内置 `image_gen` 时优先使用，无须另配图片 API Key；也可使用已确认的自备图片；只有选择第三方 `gpt-image-2` 时才需要图片接口配置。需要私密配置时，把 `shuzirenskil/.env.example` 复制到具体制作项目目录并命名为 `.env`。不要把填好内容的 `.env`、API Key、请求报文、真实视频或人物素材提交到仓库或发进公共聊天。

视频服务商配置中的请求地址是服务商公开接口路径，不包含账号凭据；使用前仍要用自己取得的官方文档核对。新模型不能只改模型名，必须核对请求字段、图片与声音输入、任务查询、费用和验收方式。

## 本地检查

在 `shuzirenskil` 文件夹运行：

```bash
python3 scripts/validate_skill.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

这些检查只验证本地文件和模拟接口，不会替你申请或验证真实服务商权限。真实连通性检查应与付费生成分开进行。
