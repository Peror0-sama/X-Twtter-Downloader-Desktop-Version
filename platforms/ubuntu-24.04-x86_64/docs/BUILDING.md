# 源码与构建

Python 应用入口为 `main.py`，主要代码位于 `xdownloader/`；C# WebView2 宿主位于 `webview_host/`。

```text
XDownloader/
  README.md / README.txt / CHANGELOG.md
  LICENSE / THIRD_PARTY_NOTICES.txt / .gitignore
  main.py / XDownloader.spec / build.ps1
  requirements.txt / requirements-dev.txt / requirements-lock.txt
  示例URLs.txt
  xdownloader/       Python 应用源码
  webview_host/      C# 网页宿主源码及构建脚本
  tests/             Python 测试与浏览器请求匹配脚本
  assets/            图标、原图和提示词
  third_party/       第三方许可证与来源说明
  docs/              构建说明和依赖清单
  release/           发布说明与本地 1.3.5 应用 ZIP
```

## 环境与依赖

开发构建使用 Windows x64、Python 3.14 x64。Python 依赖以根目录 `requirements-lock.txt` 为准。WebView2 SDK、.NET 宿主与固定浏览器版本以 `webview_host` 构建文件为准。

**重新构建需要匹配的离线依赖目录。** `release` 中的应用包不包含构建依赖。除 Python wheels 外，还需准备 FFmpeg、WebView2 CAB、.NET SDK 和 NuGet 包。

匹配依赖的文件名、SHA256 和来源记录在 [build/dependencies-manifest.json](build/dependencies-manifest.json)。实际依赖目录须在根部保留名为 `manifest.json` 的清单，以及清单所列全部文件与原有相对路径；不要混用不同版本的清单和文件。清单中的来源可用于定位材料，现有脚本不会自动下载或重建离线包。

## 构建命令

准备好依赖目录后，在源码根目录打开 PowerShell：

```powershell
.\build.ps1 -Python 'C:\实际路径\python.exe' -DependencyDirectory 'D:\实际路径\build-dependencies'
```

将两个路径替换为实际的 Python 3.14 x64 和完整依赖目录。也可以把依赖放在源码根目录的 `dependencies`，或源码父目录的 `build-dependencies`；脚本按此顺序查找。

脚本校验依赖与锁定文件的 SHA256，创建 / 同步 `.venv`，以 `--no-index` 安装 wheels，补齐 FFmpeg、解压 WebView2、编译网页宿主，运行测试，再用 PyInstaller 生成 EXE。输出为 `dist/XDownloader.exe`，同时复制到根目录 `XDownloader.exe`。

依赖齐全后无需在线拉取构建组件。首次准备 Python 可使用依赖包中的安装程序。运行应用所需组件已嵌入生成的 EXE，不需要用户安装开发工具。

## 测试

在已准备好的开发环境执行：

```powershell
$env:PATH = (Join-Path 'D:\实际路径\build-dependencies' 'node') + [IO.Path]::PathSeparator + $env:PATH
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

浏览器请求匹配测试需要 Node.js；HLS / GIF 测试需要 FFmpeg。正常 `build.ps1` 会准备这些路径和组件。

匿名命令行测试使用独立数据目录，避免与 GUI 的队列同时操作：

```powershell
.\.venv\Scripts\python.exe main.py --cli --data-dir work\cli-test --report work\report.json https://x.com/pellas00/status/2121256800184148093
```

CLI 有失败项时返回退出码 1。网络测试需使用可访问 X 的连接和有效链接。
