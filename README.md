# Cookies News Cockpit

一个独立于 Obsidian 的本地新闻驾驶舱。它在 Mac 上启动本地服务，并用默认浏览器打开白色 Cookie 主题界面；主题、关键词、门槛分数、条数与来源都由使用者自己配置。

> 当前版本为 **1.1.0**，只面向 **Intel Mac（x86_64）+ macOS Sonoma 14 或更高版本**。已知目标机是 2019 款 Intel MacBook Air。Apple Silicon 暂不在本版支持范围内。

## 它会带来什么

- Mac 使用者无需安装 Python，也无需安装 Obsidian。
- 应用数据与应用本体分开保存；替换新版 App 不会自动删除历史数据。
- DeepSeek API Key 保存在 macOS“钥匙串访问”中，不随报告或配置导出。
- 服务只监听本机回环地址，不对局域网开放。
- 手动抓取为主；驾驶舱保持打开时可进行每小时刷新。
- 首次打开可按类别选择一套中英文 RSS 来源；跳过向导时使用 4 个平衡来源。内置目录共 16 个来源，均为跨行业综合、国际、财经、科技、科学或公共机构资讯。
- 主题词支持英文逗号、中文逗号、顿号、中英文分号和换行；普通空格会保留在词组中。
- 默认只看最近 7 天，可切换为 1、3、7、14、30 天或不限日期；日期缺失的文章不会偷偷混入有限时间窗。
- 默认审核近 7 天历史及本次候选，拦截同链接或高度相似标题；标题出现实质更新时允许再次入选。
- DeepSeek 可生成“关键词扩展”草案，只有用户确认后才保存。当字面关键词零命中时，可对最多 20 条候选做受控语义兜底。
- 1.1 使用 `deepseek-v4-flash` 的非思考模式完成结构化筛选、摘要与主题建议；模型名称会在驾驶舱中明确显示。
- DeepSeek 在重试后仍不可用时，本次剩余候选会停止 AI 调用并回退关键词初筛；报告会明确区分“规则”和“AI”，不会把两种分数混在一起假装同一口径。
- 每次运行显示抓取、时效、关键词、全文、重复、AI、门槛和最终入选的真实漏斗；零结果会给出具体原因。
- 完整备份可以在另一台 Mac 上预览后安全合并。导入前自动生成本机备份；本机配置优先，DeepSeek Key 永不进入备份。单个导入包上限为 100 MiB。

## 安装（发给 Mac 使用者）

GitHub Release 会提供三个可长期直接下载的文件：

- `Cookies-News-Cockpit-<版本>-Intel.dmg`
- `Cookies-News-Cockpit-<版本>-Intel.dmg.zip`（微信对 DMG 不友好时使用）
- `SHA256SUMS.txt`（传输完整性校验）

安装步骤：

1. 如果收到的是 `.zip`，双击解压；里面只有一个 `.dmg`，没有多层 `release` 文件夹。
2. 双击打开 DMG，把 **Cookies News Cockpit** 拖到 **Applications**。
3. 在“应用程序”中首次打开。如果系统阻止：进入“系统设置 → 隐私与安全性”，确认应用名称无误后点“仍要打开”。也可以在 Finder 中按住 Control 点按 App，再选择“打开”。
4. 后续直接从“应用程序”启动。驾驶舱会在默认浏览器中打开。

### 必须知道的签名限制

1.1.0 没有 Apple Developer 证书，因此没有经过 Apple 公证。构建过程会做 **ad-hoc 完整性签名** 并验证签名，但这不等同于开发者签名或 Apple 公证。macOS 第一次运行仍会要求使用者手动批准，这是预期行为，不是安装失败。

如果 macOS 报告下载损坏，先重新下载并对照 `SHA256SUMS.txt`；不要为了绕过提示随意执行来源不明的终端命令。

## 只有 GitHub 网页端时：先构建候选包

仓库使用 GitHub 官方 Intel Mac runner，不需要身边有一台 Mac：

1. 打开仓库的 **Actions** 页面。
2. 左侧选择 **Build Intel macOS DMG**。
3. 点 **Run workflow**，输入与源码一致的版本号（当前为 `1.1.0`）并运行。
4. 等待 `Test and package x86_64 app` 变为绿色。
5. 打开本次运行，在页面底部 **Artifacts** 下载 `Cookies-News-Cockpit-<版本>-Intel`。
6. 解开 GitHub 自动生成的外层 ZIP，在目标 Intel Mac 上安装并完成实际验收。

这个 Actions artifact **只是候选测试包**：下载通常要求登录 GitHub、30 天后会过期，而且最外层是 GitHub 自动打的 ZIP。它不是应该转发给最终使用者的正式下载链接。

## 只有 GitHub 网页端时：发布正式版

请先确认候选构建为绿色，并在目标 Intel Mac 上完成安装、首次放行、启动、抓取和 DeepSeek 连接测试。然后只用 GitHub 网页完成正式发布：

1. 打开仓库的 **Releases** 页面，点 **Draft a new release**。
2. 在 **Choose a tag** 中输入与源码版本对应的标签，例如 `v1.1.0`，选择 **Create new tag: v1.1.0 on publish**；目标分支选 `main`。
3. 标题可填 `Cookies News Cockpit 1.1.0`。此时不要手动上传从 Actions 下载的外层 ZIP。
4. 点 **Publish release**。GitHub 创建标签后会自动触发 **Build Intel macOS DMG**；刚发布的 Release 可能暂时还没有安装文件，这是正常的。
5. 回到 **Actions**，等待这次由 `v1.1.0` 标签触发的运行变为绿色。发布步骤会识别网页端已存在的同名 Release，覆盖同名资产并更新标准标题和说明，不会因为 Release 已存在而冲突失败。
6. 回到 **Releases** 刷新页面，确认出现 `.dmg`、`.dmg.zip` 和 `SHA256SUMS.txt` 三个资产后，再把 Release 页面或其中的 `.dmg.zip` 链接发给使用者。

如果标签触发的运行失败，不要分享那个 Release。网络等瞬时故障可在运行页面选择 **Re-run all jobs**；如果是代码或版本问题，应修复后发布新的补丁版本，不要移动已经公开的标签。

正式 Release 资产会一直保留到维护者主动删除，并提供每个文件的直接下载入口；Actions artifact 仍只保留 30 天。推送形如 `v1.1.0` 的 Git tag 也会走同一条正式发布流水线。

流水线会执行：自动测试 → 生成 Cookie ICNS 图标 → PyInstaller `onedir/windowed` App → x86_64 架构检查 → ad-hoc 签名与验证 → DMG 压缩 → DMG 校验 → SHA-256 → artifact 上传。只有前述校验全部通过，构建出的三个正式资产才会上传到 Release；判断正式版是否可分享时，以标签构建绿色且三个资产齐全为准。

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
APP_VERSION=1.1.0 bash packaging/build_macos.sh
```

产物写入 `release/`。脚本拒绝在非 macOS 或非 x86_64 机器上打包，避免误发架构不匹配的应用。

## 数据位置

- 配置、报告、收藏与可重建索引：`~/Library/Application Support/Cookies News Cockpit`
- 运行日志：`~/Library/Logs/Cookies News Cockpit`
- DeepSeek API Key：macOS 钥匙串

删除或替换 `/Applications/Cookies News Cockpit.app` 不会自动清空上述数据。导出包不会包含 API Key。

### 更新与迁移

- **同一台 Mac 更新：**退出旧版，把新 App 拖进“应用程序”并选择替换。主题、关键词、来源、历史、收藏和钥匙串中的 Key 都留在原位置，无需重新输入。
- **换一台 Mac：**在旧 Mac 的设置中导出完整备份；在新 Mac 安装后选择“导入备份”，先看冲突预览，再执行“保留本机配置并合并”。DeepSeek Key 必须在新 Mac 的钥匙串中重新输入一次。
- **回滚：**导入前生成的自动备份保存在应用数据目录的 `backups` 文件夹。不要用复制数据库文件的方式跨版本迁移。

## 包体与微信传输

PyInstaller 使用 `onedir`，DMG 使用 UDZO 压缩。项目的保守目标是让 DMG 低于 100 MB；最终大小以每次 Actions 构建页面显示的 `du` 结果为准。`.dmg.zip` 主要用于提高聊天软件的附件兼容性，通常不会进一步大幅压缩 DMG。

## 安全边界

- 不在源码、日志、Actions artifact 或导出中保存 API Key。
- 不使用 Apple 签名密钥，也不向工作流上传证书。
- 不开放局域网端口；生产包只允许本机浏览器访问。
- 私有仓库依然应避免提交新闻站点账号、付费 Cookie、API Key 或个人数据。
- 1.1.0 只支持 RSS/Atom；普通网站首页不能直接当 RSS 使用，也没有隐藏的搜索引擎或网页爬虫。
- “每小时刷新”只在应用和驾驶舱仍运行时工作，不是关掉 App 后继续运行的系统级后台任务。

内置字体及其开放字体许可证见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
