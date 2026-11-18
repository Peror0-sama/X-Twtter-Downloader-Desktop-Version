Qt / PySide6 / Qt WebEngine 来源与 LGPL 许可选项

本包使用 PySide6-Essentials 6.12.0、PySide6-Addons 6.12.0、
PySide6-WebEngine 6.12.0.140 及 shiboken6，使用供应方提供的 LGPL 许可选项。
LGPL-3.0-only.txt 与 GPL-3.0-only.txt 为完整许可证，取自 Qt 官方仓库：
https://github.com/qt/qtbase/tree/dev/LICENSES
其他嵌入组件保留其原有许可，详见 third_party/licenses。

官方源码入口（下载与二进制版本匹配的源码及第三方来源）：
https://code.qt.io/cgit/pyside/pyside-setup.git/
https://code.qt.io/cgit/qt/qtbase.git/
https://code.qt.io/cgit/qt/qtwebengine.git/
https://download.qt.io/official_releases/qt/6.12/6.12.0/submodules/
https://download.qt.io/official_releases/QtForPython/
Qt WebEngine 含 Chromium 及其第三方组件，源码和构建依赖在其源码树中说明。

库未作修改。应用提供全部原创源码、requirements.txt、XDownloader.spec 与 build.ps1。
替换库：按 README 建立虚拟环境，安装兼容的修改版 Qt/PySide6 wheel，
然后使用 XDownloader.spec 重新构建 EXE。修改版需要包含所用的 Qt WebEngine 模块。
本应用没有签名校验、激活或 DRM 来阻止运行重新构建的版本；无需发布者密钥。
可直接从源码运行 main.py 验证替换的库。第三方库修改、调试和为此所需的逆向工程
不受本项目非商业许可限制；第三方组件可按其原许可独立使用和再分发。
