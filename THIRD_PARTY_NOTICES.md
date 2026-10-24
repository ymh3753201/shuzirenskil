# 本地运行环境的第三方组件

本仓库发布自身源码、版本清单和安装程序。首次安装从下列来源下载工具与模型；不会分发作者的密钥、账号或虚拟环境。

| 组件 | 来源与版本 | 用途 |
|---|---|---|
| Python | python.org；uv 管理时为 python-build-standalone 的 Python 3.12 | 独立运行环境 |
| uv | https://github.com/astral-sh/uv ，0.9.5 | 仅在当前解释器不是 3.12 时准备环境 |
| faster-whisper | https://github.com/SYSTRAN/faster-whisper ，1.2.1 | 本地语音识别 |
| CTranslate2、PyAV、ONNX Runtime、NumPy 等 | 固定版本见 requirements-runtime.txt，实际安装全清单在运行环境 installed-requirements.txt | CPU 推理与音频解码 |
| Silero VAD | faster-whisper 包内的本地模型，https://github.com/snakers4/silero-vad | 语音活动区间检测 |
| small 多语言识别模型 | https://huggingface.co/Systran/faster-whisper-small/tree/536b0662742c02347bc0e980a01041f333bce120 | OpenAI Whisper small 的 CTranslate2 转换权重 |
| keyring | https://github.com/jaraco/keyring ，25.6.0 | 系统凭据库 |
| FFmpeg / ffprobe | 优先使用本机已安装版本；缺少时由 static-ffmpeg 3.0 从其 ffmpeg_bins v8.0 对应系统下载 | 媒体解码、剪辑、合成 |

各第三方组件保持自身许可证。wheel 内的 dist-info 许可证及模型卡应保留；依赖缓存保持 wheel 原文件。FFmpeg 可执行文件不进入本仓库或依赖缓存，安装时来源见 https://github.com/zackees/static_ffmpeg 和 https://ffmpeg.org/download.html 。

缓存仅用于对应系统/CPU/Python；不是完整操作系统或全平台离线软件包。本地推理不向第三方发送作品。识别模型不是会话多模态工具，不能代替真实的音色、口型和动态画面验收。
