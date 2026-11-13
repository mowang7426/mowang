# 正式 R2 软件源维护

正式独立前缀：https://pub-f47a5a9b45724684be0f3f006e95e609.r2.dev/repo/

索引、相对 Filename `debs/<SHA256>.deb`、图标、横幅和 depiction 全在该前缀。首次发布先完整 list 审计 `repo/`；非空且无本发布器 ownership.json 时拒绝写入，绝不覆盖桶根、smoke 或 test-runs。已有外部同哈希包可以全字节验证后引用，但 manifest 必须标 external，不能自动纳管。仅本流程新建并具有 owner/owner-version 元数据，或此前成功 manifest 明确托管且元数据一致的对象可标 managed。外部索引/资源禁止覆盖。

原 build.sh、validate-repo.sh、build.yml 的测试、并发及回写保护不变，GitHub debs 不改。索引只替换 Filename；保留原 Package/Version/Architecture/Size/SHA256 和历史 UTF-8 surrogateescape 字节，LF 分帧。Release 含莫忘专属源及 Components: main。

## 上传与自动下架

main 中 debs/ 的新增、替换、删除触发完整校验和发布。每个 unique hash 均 HEAD + S3 GET + public GET 核验 size/SHA256，全部包完成后发布资源/索引，Release 最后，再回读索引和保存成功清单。共享 hash 按引用数保留。正式物理 GC **禁用**，GitHub 删包只从下一代索引自动下架，不从 R2 物理删除。定时任务仅重验/同步并产生建议计划，不执行包 DELETE。

保留 immutable snapshots/<commit>/ 和前三代引用。48 小时 tombstone 仅供未来人工审核计划参考，不是删除授权。没有可执行的生产物理 GC workflow；将来启用需另行人工审核与重新核验全部历史引用及归属。不得声称 S3 锁能锁住 GitHub 提交或实现跨系统原子性。

所有 publisher 同 Actions 队列、S3 条件锁，不取消运行；每个关键阶段重查 branch freshness，过期运行不能发布。main 和 S3 多对象 PUT 不原子，发布窗口可能 checksum mismatch，重试可恢复。包永不自动物理删除，旧索引仍可获取已上传对象。异常杀进程可能留锁，需维护者确认无存活 publisher 后处理，不能自动抢锁。锁删除仅为协调，不是包 GC。

PR 无 secrets。仅 main，或用户显式 workflow_dispatch、固定受信分支 feat/r2-deb-publish 且 prepublish=true，才能获取凭据并写同一 repo/ 前缀。预发布必须核对当前 main 全部 debs tree 相同。每个新 commit 都须重新 CI，不沿用旧结果。

## 旧源与 Pages

不把新的轻量相对索引部署到旧 Pages，以免旧相对 debs 路径损坏。旧 Pages 已部署内容不删除；旧 URL 不会自动切换为 R2，也不承诺以后继续更新。此 workflow 不再负责 Pages 部署，这是明确的行为变更。原 build.yml 仍保留并继续构建/回写原 GitHub 索引。Pages 若仍由仓库 legacy 自动构建大包，可能失败，需如实报告，不能称全部 Actions 绿。用户需在 Sileo 另加新正式 URL；确认后可自行移除旧源避免重复，不能删 GitHub debs。

Release 仍未签名，没有假造 GPG 信任链；隔离 APT 验证使用 trusted=yes，仅测试下载不安装，不等于密码学认证。r2.dev 有开发域速率限制，无 SLA；不购域名。测试源真机截图不等于此正式 URL 真机验收。
