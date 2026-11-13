# 正式 R2 软件源维护

新源：https://pub-f47a5a9b45724684be0f3f006e95e609.r2.dev/

索引、debs/<SHA256>.deb、图标、横幅和 depiction 全在同一 origin。Filename 只能是相对路径。Sileo Release 必须包含 Components: main。原 build.sh、validate-repo.sh、build.yml 和 GitHub 仓库 debs 原样保留；新发布流程先执行原构建和完整校验，再复制原 Packages 仅改 Filename（UTF-8 surrogateescape，LF 分帧）。

## 日常上传、删除

通过 GitHub 上传、替换或删除 debs 中的文件并提交 main 即触发正式 workflow。PR 和非 main workflow_dispatch 只全量校验，不接触 secrets、不写 R2。默认 GitHub checkout 是完整的，不使用本地稀疏布局。原仓库索引回写保护独立保留。

正式发布逐 unique hash 条件创建对象（不覆盖既有不同内容），检查 S3 HEAD 的 SHA256/size、S3 GET 全字节和公网 GET 全字节，逐文件核对引用与 Packages。资源和索引先发布，Release 最后发布并回读所有索引，再保存 managed-manifest.json。无 S3 多对象原子性承诺：刷新窗口可能临时 checksum mismatch，重试可恢复。snapshots/<commit>/ 保留历史索引，缓存的旧根索引客户端仍有 48 小时包保留期；超过该窗口不能保证旧索引下载。源目前沿用未签名 Release，未新增/谎称 GPG 信任链。

## 自动清理严格边界

只根据上次成功 managed manifest 和当前 manifest 的差集接管删除，限定 debs/<64hex>.deb。共享 hash 按引用数保留。新无引用对象记录 tombstone，至少 48 小时后、一次成功完整发布后才可删除。每天 UTC 03:23 的 Actions 或下一次 main 变更执行到期清理；GitHub 定时任务可能延迟。首次无旧 manifest 时没有候选，不接管桶内既有 smoke、test-runs/ 或其他对象。禁止 list+批量删除、sync --delete、空目录清空桶。

main commit freshness 每个关键阶段和每个删除前重查；Actions 固定 concurrency group 不取消运行，另有 S3 IfNoneMatch 条件创建 production-publish.lock 分布式门闩。任何上传/校验/主线变更失败都不进入清理。进程异常被杀可能留下 lock；不自动超时抢锁，须确认没有存活 publisher 后由维护者处理。锁对象删除属于协调，不是 DEB 清理。

## 旧源与 Pages

不再部署 _site 到 GitHub Pages，避免发布不含 DEB 的相对索引造成旧 URL 下载 404。已有 Pages 部署保持不动；旧 URL 不会自动变为 R2、也不保证继续更新。用户需另外添加上述新 URL，确认正式源真机显示后可在客户端移除旧源以避免重复；不要删除 GitHub 仓库 debs。若未来重新部署 Pages，只可部署小型介绍页/图片，不能拿 R2 的相对索引替换旧 Pages 源。

r2.dev 是有速率限制的开发地址，不是 SLA 生产域名；此次不购域、不部署付费代理。不把隔离测试源 Sileo 截图当正式源真机验收。
