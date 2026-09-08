# Cookies News Cockpit

一个独立于 Obsidian 的本地新闻驾驶舱。它在 Mac 上启动本地服务，并用默认浏览器打开白色 Cookie 主题界面；主题、关键词、门槛分数、条数与来源都由使用者自己配置。

> 当前为 V1 开发版，只面向 **Intel Mac（x86_64）+ macOS Sonoma 14 或更高版本**。已知目标机是 2019 款 Intel MacBook Air。Apple Silicon 暂不在本版支持范围内。

## 它会带来什么

- Mac 使用者无需安装 Python，也无需安装 Obsidian。
- 应用数据与应用本体分开保存；替换新版 App 不会自动删除历史数据。
- DeepSeek API Key 保存在 macOS“钥匙串访问”中，不随报告或配置导出。
- 服务只监听本机回环地址，不对局域网开放。
- 手动抓取为主；驾驶舱保持打开时可进行每小时刷新。
- 默认审核近 7 天历史及本次候选，拦截同链接或高度相似标题；标题出现实质更新时允许再次入选。
- DeepSeek 在重试后仍不可用时，本次剩余候选会停止 AI 调用并回退关键词初筛，避免故障放大为数百次慢请求。

## 安装（发给 Mac 使用者）

GitHub Actions 会生成三个文件：

- `Cookies-News-Cockpit-<版本>-Intel.dmg`
- `Cookies-News-Cockpit-<版本>-Intel.dmg.zip`（微信对 DMG 不友好时使用）
- `SHA256SUMS.txt`（传输完整性校验）

安装步骤：

1. 如果收到的是 `.zip`，双击解压得到 `.dmg`。
2. 双击打开 DMG，把 **Cookies News Cockpit** 拖到 **Applications**。
3. 在“应用程序”中首次打开。如果系统阻止：进入“系统设置 → 隐私与安全性”，确认应用名称无误后点“仍要打开”。也可以在 Finder 中按住 Control 点按 App，再选择“打开”。
4. 后续直接从“应用程序”启动。驾驶舱会在默认浏览器中打开。

### 必须知道的签名限制

V1 没有 Apple Developer 证书，因此没有经过 Apple 公证。构建过程会做 **ad-hoc 完整性签名** 并验证签名，但这不等同于开发者签名或 Apple 公证。macOS 第一次运行仍会要求使用者手动批准，这是预期行为，不是安装失败。

如果 macOS 报告下载损坏，先重新下载并对照 `SHA256SUMS.txt`；不要为了绕过提示随意执行来源不明的终端命令。

## 在 GitHub 网页端构建 Intel 安装包

仓库使用 GitHub 官方 Intel Mac runner，不需要身边有一台 Mac：

1. 打开仓库的 **Actions** 页面。
2. 左侧选择 **Build Intel macOS DMG**。
3. 点 **Run workflow**，输入版本号（例如 `0.1.0`）并运行。
4. 等待 `Test and package x86_64 app` 变为绿色。
5. 打开本次运行，在页面底部 **Artifacts** 下载 `Cookies-News-Cockpit-<版本>-Intel`。
6. 解压 Actions artifact 后，把 DMG 或 `.dmg.zip` 通过微信发送给目标 Mac。

推送形如 `v0.1.0` 的 Git tag 也会自动触发同一构建。Actions artifact 默认保留 30 天。

流水线会执行：自动测试 → 生成 Cookie ICNS 图标 → PyInstaller `onedir/windowed` App → x86_64 架构检查 → ad-hoc 签名与验证 → DMG 压缩 → DMG 校验 → SHA-256 → artifact 上传。任何一步失败都不会发布半成品。

## 本地开发

开发环境要求 Python 3.12：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest
cookies-news-cockpit
```

仅在 Intel macOS 上构建安装包：

```bash
APP_VERSION=0.1.0 bash packaging/build_macos.sh
```

产物写入 `release/`。脚本拒绝在非 macOS 或非 x86_64 机器上打包，避免误发架构不匹配的应用。

## 数据位置

- 配置、报告、收藏与可重建索引：`~/Library/Application Support/Cookies News Cockpit`
- 运行日志：`~/Library/Logs/Cookies News Cockpit`
- DeepSeek API Key：macOS 钥匙串

删除或替换 `/Applications/Cookies News Cockpit.app` 不会自动清空上述数据。导出包不会包含 API Key。

## 包体与微信传输

PyInstaller 使用 `onedir`，DMG 使用 UDZO 压缩。项目的保守目标是让 DMG 低于 100 MB；最终大小以每次 Actions 构建页面显示的 `du` 结果为准。`.dmg.zip` 主要用于提高聊天软件的附件兼容性，通常不会进一步大幅压缩 DMG。

## 安全边界

- 不在源码、日志、Actions artifact 或导出中保存 API Key。
- 不使用 Apple 签名密钥，也不向工作流上传证书。
- 不开放局域网端口；生产包只允许本机浏览器访问。
- 私有仓库依然应避免提交新闻站点账号、付费 Cookie、API Key 或个人数据。

内置字体及其开放字体许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
