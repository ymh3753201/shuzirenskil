# 数字人口播视频 Skill（shuzirenskil）

当前版本：**2026.10.24-rc6，受控试用**。供 Codex 制作数字人口播、门店或产品广告：先确认稿件和方案，再确认真实参考图，最后按授权范围生成和验收。

已登记 Grok 网关、MiniMax H3/H3 Max、Seedance 2.0/2.5、Wan 3、Veo 3.1 等适配。不同服务商的接口和权限分别核对；本地测试通过不代表所有模型、账号或成片效果均已验证。详见 [上线范围](shuzirenskil/references/release-readiness.md)。

## 零基础安装

1. 下载 [仓库 ZIP](https://github.com/ymh3753201/shuzirenskil/archive/refs/heads/main.zip)，或使用作者提供的新版完整分享包，**先完整解压**。GitHub 解压目录通常叫 `shuzirenskil-main`；请选择其中同时包含 `install.py`、`MANIFEST.sha256.json` 和 `shuzirenskil` 文件夹的目录。
2. 在 Codex 中打开自己的视频项目文件夹。不要把下载的安装包目录当作视频项目。
3. 复制 [给 AI 的安装提示词](给AI的安装提示词.txt)，填入解压目录和视频项目路径，发给 Codex。
4. 让 Codex 按 [安装和使用说明](安装和使用说明.md) 完成检查和安装，再新开该项目的 Codex 会话确认能读取技能。

标准安装位置为 `视频项目/.agents/skills/shuzirenskil`。推荐使用包内 `install.py`：它校验全部核心文件、拒绝覆盖已有版本，失败时不留下半份技能。Windows/macOS 脚本只是同一安装器的入口，不需要各运行一遍。安装过程不生成图片或视频。

## 环境与配置

- `install.py --with-runtime` 自动准备隔离 Python 3.12、FFmpeg/ffprobe（缺少时）、固定版本的本地语音识别依赖和 small 多语言模型，并运行实际自检。首次需联网下载；不上传素材、不调用付费生成。已有可运行的 Python 3.10+ 可启动安装器。
- 后续统一用 `python scripts/run.py doctor|studio|workflow|audio_qc|credential_store ...`，不要求手动激活环境。通用包是联网安装包，不冒充全平台离线软件。详见 [自动质检和环境说明](shuzirenskil/references/automatic-quality.md)。
- 视频制作需要使用者自己的服务商账号和密钥。图片优先使用当前会话可用的 Codex 内置生图或已有图片；仅第三方图片路线需要另配图片接口。
- 新用户优先将密钥保存到系统凭据库：Windows Credential Manager、macOS Keychain。按 [配置与排错指南](shuzirenskil/references/setup-and-troubleshooting.md) 完成隐藏输入、保存、回读和新进程检查；不要把创建空 `.env` 当作配置完成。
- 旧环境变量和私密 `.env` 保持兼容，可能优先于凭据库。不要把真实密钥写进聊天、命令参数、仓库或分享包。

## 安装完成后

请从 [使用说明](安装和使用说明.md) 进入模型配置与视频方案。普通时长默认只作参考，以内容自然完成为准；保留已确认的语速、音色和表情动作。用户确认制作后不强制先报价；另有明确金额上限时遵守上限。多段先验首段，提交结果不明时只查原任务，不自动重发。

需要手动安装、升级、回退或处理失败，见 [安装和使用说明](安装和使用说明.md)。接入其他模型，见 [接口接入规范](shuzirenskil/references/provider-integration.md)，不能只替换模型名称。

## 验证范围

rc6 新增安全裁尾、音频检查、20/30 秒一次授权推进与跨平台锁测试，并增加 Windows/macOS/Linux 持续集成。实际结果以本版本测试记录及 GitHub Actions 为准；自动化通过不等于学员电脑和全部模型效果已验收。代码、免费接口查询、真实首段和全片验收分别记录，不能互相替代。
