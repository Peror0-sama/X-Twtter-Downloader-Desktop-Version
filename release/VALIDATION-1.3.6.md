# 1.3.6 验证记录

- 修复了内嵌网页无法输入的问题。
- Windows x64：159 项测试通过；真实 X 登录页键盘输入已验证。
- Windows x86：7 项登录焦点测试通过；全量测试在 test_batch_prompt_offers_individual_mode 处进程退出，未宣称全量通过。
- Ubuntu 24.04：7 项登录焦点测试通过；全量运行 159 项、6 项跳过，系统密钥环测试因 WSL 未解锁密钥环失败。
- x86 与 Ubuntu 未完成实际账号登录验证；应在对应系统使用真实账号验证登录与状态保存。
- 构建使用本机可用 Python/.NET/PyInstaller；不是原版锁定构建环境的逐字节复现。
- Electron、WebView2 与 FFmpeg 运行时复用 1.3.5 对应平台的发布组件。
