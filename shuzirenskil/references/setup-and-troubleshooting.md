# 首次安装、密钥和接口检查

先识别操作系统、当前项目、Python 3.10+、FFmpeg/ffprobe。Windows 检查 `py -3` 或 `python`，macOS/Linux 检查 `python3`；后续安装依赖与运行脚本必须使用同一个解释器。路径带空格或中文要正确引用。现有安装先备份，配置与素材不覆盖。

## 密钥保存

新用户优先使用系统凭据库：Windows Credential Manager、macOS Keychain、Linux Secret Service。不要只创建空 `.env` 就说配置完成。不要把用户已提供的密钥再次贴进聊天、命令参数、源码、报告或飞书。

在同一 Python 环境安装 `keyring`，再在本机交互终端运行 `scripts/credential_store.py set --name 变量名`。程序隐藏输入、保存到系统凭据库并回读校验；已有值不会覆盖，用户明确要更新时用 `--replace`。随后新开一个进程运行 `scripts/credential_store.py status --name 变量名`，只报告是否配置。若系统凭据库不可用，明确说明缺项，不静默保存明文。不要为了在无交互工具中运行而关闭隐藏输入；向用户展示本机终端输入入口。

变量：AI 跃云 Grok 为 `SHUZIRENSKIL_AIHGE_API_KEY`；MiniMax 为 `MINIMAX_API_KEY`；火山方舟为 `ARK_API_KEY`；91topgo 为 `SHUZIRENSKIL_91TOPGO_API_KEY`；Veo 为 `GEMINI_API_KEY`。各服务密钥分别存储，不共用通用变量。API 地址保存在非密钥配置中，不能把同一 Key 转发到另一主机。

旧的环境变量和显式 `.env` 继续兼容；它们可能优先于凭据库，切换配置时检查来源，但不打印值。macOS 旧的 91topgo 钥匙串入口也保留。确需保留旧 `.env` 时：macOS/Linux 仅当前用户可读写（600）；Windows 检查 NTFS ACL，仅保留当前用户与必要系统账户访问，不能以 chmod 600 代替 Windows 权限检查；不要存入公开仓库、网盘共享目录或安装包。

## 模型与检查

先记录服务商、准确模型 ID、API 根地址、输入方式和接口文档。默认只做本地检查与已知免费的只读查询。模型列表可见不等于生成成功；首段制作仍按现有确认与验收流程执行。

- MiniMax H3/H3 Max：继续使用原有官方 v2 协议。
- Seedance 2.0：新增 `seedance20-reference`，模型 `doubao-seedance-2-0-260128`，保守支持 4–15 秒、最多 9 张参考图；现有 Seedance 2.5 保留。2.0 不继承 2.5 的 30 秒与视频续接合同。
- AI 跃云 Grok：新增 `aihge-grok-reference`，仅绑定 `https://api-hk.aihge.com` 与专用密钥；使用 `duration`、`resolution`、`aspect_ratio`，读取创建响应正文的 `request_id`，支持完成状态 `done`。已实测仅为纯文字 15 秒 480p；参考图输入和数字人效果仍须首段验证，不能宣称全部模式已验收。
- 其他中转：逐一核对协议；不能只改模型名、绕过域名检查或启用测试模式访问真实接口。

## 没有任务编号时

先查看本地账本和脱敏诊断。分清网络超时、HTTP 错误、业务错误、缺少约定编号；不要将它们都解释为平台没建任务。响应头追踪号与正文任务编号分开。保留已有编号，只查询原任务；提交结果不明时不自动重发。不要要求用户先去找一个可能根本不存在的任务编号。支持人员只需脱敏错误、模型/参数/时间与请求追踪号，不需要密钥或私人图片。

安装通过、凭据已保存、免费接口检查通过、真实首段通过分别报告。Windows 真机未测试时必须如实注明，不能用模拟测试替代。
