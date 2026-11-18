# XDownloader 1.3.5

X / Twitter 图片、视频、动图及广播回放下载器。制作人：**Peror0_sama**。

## 下载与使用

三个平台的安装包统一存放在 [release](release/)。

| 平台 | 安装包 | 源码与说明 |
| --- | --- | --- |
| Windows 10/11 x64 | [Windows x64](release/XDownloader-1.3.5-Windows-x64.zip) | [windows-x64](platforms/windows-x64/) |
| Windows 10 x86（32 位） | [Windows x86](release/XDownloader-1.3.5-Windows-x86.zip) | [windows-x86](platforms/windows-x86/) |
| Ubuntu 24.04 Desktop x86_64 | [Ubuntu x86_64](release/XDownloader-1.3.5-Ubuntu-24.04-x86_64.zip) | [ubuntu-24.04-x86_64](platforms/ubuntu-24.04-x86_64/) |

Windows：完整解压后运行 `XDownloader/XDownloader.exe`。

Ubuntu：完整解压后安装其中的 `.deb`，然后从应用菜单启动 XDownloader。

兼容版的运行环境及验证情况见各自的 `兼容性说明.txt`。源码目录不重复附带外部运行时和构建缓存。

## 目录

```text
XDownloader-GitHub/
├── README.md
├── README.txt
├── LICENSE
├── CHANGELOG.md
├── platforms/
│   ├── windows-x64/
│   ├── windows-x86/
│   └── ubuntu-24.04-x86_64/
└── release/
    ├── README.md
    └── 三个平台的 ZIP 安装包
```

## 上传 GitHub

源码和文档上传到仓库；`release` 中的三个 ZIP 上传到同一个 Release 的附件区域。`.gitignore` 已将安装包排除在源码提交之外。

## 许可

原创代码采用 [XDownloader 非商业源码共享许可 1.0](LICENSE)。第三方组件按各自许可使用，说明保留在各平台目录中。
