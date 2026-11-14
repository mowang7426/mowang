# 永久管理记录与 Deb 上传收件箱：第一阶段（草案，不可上线）

## 当前状态与安全承诺

本分支提供永久 JSON 记录、全量初始导入证明、追加式已处理账本、只读索引生成器、只读清理计划和清理安全门的纯函数测试。**尚未接入生产发布链，不可合并或启用清理。** 当前线上发布器和旧 Build MoWang Repo 仍以 debs 全集构建。保持现状是为防止未完成的迁移把正常源写空，而不是认可旧 source-of-truth 设计。

- 没有删除现有 Deb，没有删除 R2 包对象，没有修改 Git 历史。
- 没有新增任何自动清理 job，也没有开启 DEB_INBOX_CLEANUP_ENABLED。
- `catalog-gates.py` 只计算允许列表；没有 git rm、GitHub 删除 API 或 R2 DELETE 执行器。
- 用户当前不能通过本分支删除 JSON 改变正式源；生产迁移完成后才能提供该行为。
- `https://mowang-repo.pages.dev/`、R2 主源、Origin/Label「莫忘专属备份源」、现有用户横幅与旧 GitHub Pages 全部不动。

## 文件与事实源

- `packages/*.json`：一条历史来源对应一条永久管理记录，保存 Package、Version、Architecture、SHA256、Size、不可变 `debs/<完整sha256>.deb` 对象键、原始 metadata base64 和原字节 SHA256，以及原 Filename base64、Git blob。
- 文件名为可读包名/版本/架构短 slug + 内容 hash 前缀 + 来源 hash 前缀，避免中文超长路径。同 identity 不同内容、同内容多文件、多版本和多架构均保留独立记录。截断只用于文件名，完整值保存在 JSON；ID 碰撞导入直接失败。
- `metadata_b64` 保留完整原控制段，包含非 UTF-8 历史字节。渲染仅替换 `Filename`，不重编码其他字段。记录不是秘密，不含二进制。
- `catalog-state/package.schema.json` 为结构 schema；`scripts/catalog.py` 负责跨字段一致性、严格相对路径、原始字节解析、重复字段、base64、记录名和账本绑定验证。不能只做 JSON Schema 验证。
- `catalog-state/processed.json`：追加式 ledger，以「完整原路径字节 + Git blob」标识已处理上传，永久保留已下架记录映射。删除记录时不能删除对应 ledger。旧 Deb 还留着也不应复活；同路径新 blob 是新上传。
- `packages/README.md`：自动生成中文查找表，不是事实源。
- `catalog-state/bootstrap-proof.json`：初始快照及全量核验摘要。
- 根 `Packages` 仍为旧工作流产物，本草案不改写；将来应清楚标为正式 R2 索引副本，不能假定相对 Filename 在 Git 工作树上存在。

## 初始导入核验

`bootstrap-catalog.py` 读取可信提交的 `Packages`、Git debs 树、当前正式 R2 `Packages` 和 `managed-manifest.json`。逐条核对 metadata（除 Filename）、原文件映射、SHA256、Size、引用计数和完整对象集合，拒绝隐式去重。

对所有 R2 内容对象进行全量 GET，流式计算 SHA256；同时重建 Git SHA1：`SHA1("blob " + Size + NUL + bytes)`，与可信 Git 树 blob 比较。这在部分克隆中不用下载一份完整 Git 历史，也能证明回读字节就是原 blob。不会在磁盘保存 3GB Deb。

导入输出必须是新目录。默认模式先完整验证，失败不生成记录。另有明确标记的 `--prepare-draft` 模式，仅准备草案：其证明为 `mode=draft-preparation`、`git_blob_full_hash_verified=false`、`production_r2_get_reverification=false`、`production_ready=false`，绝不是发布证据；独立无凭据 CI 必须用默认全量模式重新验证。初始公共 GET 证明不是生产发布收据；正式发布时仍必须保留 S3 HEAD 元数据/所有权/Size 和全部 GET hash 校验，不能凭该历史证明跳过。

只读使用：

```sh
python3 scripts/test-catalog.py
python3 scripts/catalog.py validate
python3 scripts/catalog.py render --output /tmp/catalog.Packages
python3 scripts/catalog.py plan --output /tmp/cleanup-plan.json
python3 scripts/catalog-docs.py
```

## 上线前必须完成的状态机（本草案未声称已实现）

1. workflow 取得可信 main snapshot，记录 base SHA、debs tree、catalog/ledger tree；保持全生产发布 concurrency 串行、R2 分布式锁和 freshness 门。
2. 原 build 对每个新上传做真实 Debian ar/control/data 归档检查，不能为兼容历史数据放宽任意坏包；只处理 ledger 未见过的路径+blob。
3. 上传不可变对象，完成 S3 HEAD metadata/所有权/Size + 全量 GET SHA256；生成新记录与追加 ledger。用户删除的旧记录禁止根据仍在 inbox 的旧 blob 自动重建。
4. 用 CAS 把新增 JSON + ledger 提交到可信 main；失败不得发布。不能覆盖用户在此期间删记录或替换 Deb 的提交。
5. 当前 job 显式重新载入刚成功提交的 catalog 构建全部索引并发布；不能依赖 GITHUB_TOKEN push 自动触发新 workflow。发版前再次核对 main 与 catalog commit，旧 task 不能覆盖新 task。
6. 所有在架 catalog 对象每次发布仍做完整回读。根索引不依赖本地 Deb 是否存在；Release 最后写、发布后独立回读。完整收据必须绑定 commit、catalog hash、index hash、每个对象的 HEAD/GET 证明和成功状态。
7. 自动收件箱清理默认禁用，缺失或 false 均关闭。首次现有 Deb 批量计划必须向用户列出精确清单、数量、字节数、base SHA、tree SHA，再单独取得二次确认；不能因为用户同意新架构而推定同意首次删除。
8. 实际执行器只能删除成功收据 + 同 catalog 记录/hash + 完整 GET 证据覆盖的精确路径/blob。删除前重新获取 current main，检查同 base/同 blob，CAS 新 tree 提交，禁止通配、force 和覆盖并发新文件。提交后刷新收据/计划，重试也不能误删。
9. 删除 JSON 只会索引下架，R2 物理 GC 永久保持禁用。空 catalog 默认 fail closed；全部下架需设计独立审批协议后才能开放。恢复记录使用 Git 历史和仍保留的 R2 对象；不能悄悄重建 tombstone。
10. 旧 Build MoWang Repo、build.sh、validate-repo.sh、verify-r2-public.py 与正式 publisher 必须一起适配 catalog + 新上传 inbox。不得仅关闭旧工作流或先清空 debs。

## 测试及合并阻塞

纯函数测试覆盖非 UTF-8 保留、所有来源映射、多版本/架构/同 identity 内容变化、重复引用、非法路径/key/metadata、删记录旧 inbox 不复活、空 inbox 保持全部索引、账本不可重写、默认关闭与字符串 false、精确审批、发布失败/缺失全 GET 证明、catalog/commit/blob 并发改变拒绝清理。原有回归不删不改。

仍需实际完成并验收：新上传自动入库提交事务、生产 catalog 构建发布接入、旧 build 无 Deb 兼容、真实 HEAD 全量证明、可信收据传递、隔离 R2 testprefix + GitHub 测试分支两包完整 E2E、生产候选索引逐字节对比、PR 全 CI 与 main 发布及独立小/大包下载验收。完成前本 PR 必须保持 Draft。

现有只读 `cleanup-plan.json` **不是可执行授权，也不是成功发布收据**。首次清理只能在上述阻塞全部解除、生产发布成功后重新以最新 main 生成计划，由主代理向用户二次确认。删除 GitHub 中转文件不会缩小已有 Git 历史，本阶段不做 history rewrite。
