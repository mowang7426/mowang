# 永久管理记录与 Deb 上传收件箱：catalog 生产集成

## 当前状态与安全承诺

生产链已接通：真实归档验证 → R2 不可变上传与完整回读 → JSON/ledger 非强制 Git CAS → 同 job 显式 checkout 成功提交 → catalog 构建 → 完整 HEAD/S3/public GET → Release/manifest 发布 → 可信收据。旧 Build MoWang Repo 在 catalog 模式只验证、不回写竞争提交。PR #11 必须在最新全量 CI 与真实隔离 E2E 成功后才合并；正式上线以 main 发布成功及独立验收为准。

**自动收件箱删除器尚未实现，保持禁用；R2 物理 GC 禁用。它们不阻塞普通 catalog 管理上线。**

- 没有删除现有 Deb，没有删除 R2 包对象，没有修改 Git 历史。
- 没有新增任何自动清理 job，也没有开启 DEB_INBOX_CLEANUP_ENABLED。
- `catalog-gates.py` 只计算允许列表；没有 git rm、GitHub 删除 API 或 R2 DELETE 执行器。
- main catalog 模式发布成功后，删除某条 `packages/*.json` 并提交即可下架该记录；等待工作流成功后核对正式索引。不要删除其 ledger 项。二进制仍保留在 R2，可经审查恢复原记录。空 catalog 全部下架暂时 fail-closed。
- 新上传仍提交到 `debs/`；PR 只校验归档及模拟 prospective catalog，不接触秘密、不提前要求未上传对象存在。main 发布阶段才真实 import-CAS，同 job 重新载入成功 commit。bot token 提交无需依赖自动触发新 workflow。
- `https://mowang-repo.pages.dev/`、R2 主源、Origin/Label「莫忘专属备份源」、现有用户横幅与旧 GitHub Pages 全部不动。

## 文件与事实源

- `packages/*.json`：一条历史来源对应一条永久管理记录，保存 Package、Version、Architecture、SHA256、Size、不可变 `debs/<完整sha256>.deb` 对象键、原始 metadata base64 和原字节 SHA256，以及原 Filename base64、Git blob。
- 文件名为可读包名/版本/架构短 slug + 内容 hash 前缀 + 来源 hash 前缀，避免中文超长路径。同 identity 不同内容、同内容多文件、多版本和多架构均保留独立记录。截断只用于文件名，完整值保存在 JSON；ID 碰撞导入直接失败。
- `metadata_b64` 保留完整原控制段，包含非 UTF-8 历史字节。渲染仅替换 `Filename`，不重编码其他字段。记录不是秘密，不含二进制。
- `catalog-state/package.schema.json` 为结构 schema；`scripts/catalog.py` 负责跨字段一致性、严格相对路径、原始字节解析、重复字段、base64、记录名和账本绑定验证。不能只做 JSON Schema 验证。
- `catalog-state/processed.json`：追加式 ledger，以「完整原路径字节 + Git blob」标识已处理上传，永久保留已下架记录映射。删除记录时不能删除对应 ledger。旧 Deb 还留着也不应复活；同路径新 blob 是新上传。
- `packages/README.md`：自动生成中文查找表，不是事实源。
- `catalog-state/bootstrap-proof.json`：初始快照及全量核验摘要。
- 根 `Packages` 在构建时由 catalog 渲染；Git 中历史索引不是正式事实源，旧 build 不再回写 bot 索引提交。正式索引以 R2 / 短域及发布收据为准，relative Filename 指向不可变对象，不要求本地 Deb 存在。

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

## 已实现的发布状态机与仍禁用的删除执行器

以下 1–6、9–10 已接入生产链；7–8 是未来清理执行器必须满足的规范，**本阶段没有实现删除器、没有启用清理、没有首次清空授权**。

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

最新集成回归 185 项通过（原 177 + 新 8），保留原 17 项竞态 guard。最新代码 CI 同时执行全部 1439 个原 Deb 的 control/data 校验、真实 APT 下载和全量 1407 R2 对象双哈希覆盖。每次改动仍须等待对应新 commit 的 CI 结果，不能借用旧绿。

真实隔离 E2E 已通过：[43544659449](https://github.com/mowang7426/mowang/actions/runs/43544659449)，Artifact `14093945833`。独立分支及前缀 `catalog-e2e/pr11-claim-v1/`，两个无维护脚本真实 Deb：production import-CAS 新包后发布 2 条；移除测试 inbox 后仍发布 2 条；删除一条 JSON 且重现旧 inbox 后 import=0、发布 1 条，R2 原对象仍存在。三阶段均有 production publisher 真实 HEAD/S3/public GET 和可信 immutable receipt，不是 mock。没有操作 main/debs 或正式 repo/。

现有只读 `cleanup-plan.json` **不是可执行授权，也不是成功发布收据**。首次清理只能在上述阻塞全部解除、生产发布成功后重新以最新 main 生成计划，由主代理向用户二次确认。删除 GitHub 中转文件不会缩小已有 Git 历史，本阶段不做 history rewrite。

## 已完成的真实验收证据（不是生产发布）

- PR：https://github.com/mowang7426/mowang/pull/11 。在最新 commit 全量 CI、真实隔离 E2E 与无 debs diff 条件均通过后可 Ready/merge；首次清理仍需独立二次确认。
- 全量只读核验成功：https://github.com/mowang7426/mowang/actions/runs/43537235893 ，job 132269073506，Artifact 14090695846。检查的是 head `706948e836b909be3057d8040a3ea814a47f5eed` 对应 PR merge commit `e1a667f8865252f882007809a296a1ac80432220`。证明已写入 `catalog-state/bootstrap-proof.json` 和 `verification-workflow.json`。
- 1439 条来源记录、1439 项精确 path+blob ledger；1407 个对象全部 GET 完整 SHA256 与原 Git blob SHA1 验证成功。原 metadata 除 Filename 外字节多重集与正式源完全一致；每条生成 JSON 与已提交记录逐文件比较通过。
- 唯一对象字节 3448558620；保留全部来源引用的文件字节 3511157520。版本、架构与同 identity 记录不擅合并。
- 本地原 71 项测试与新增 62 项（51 catalog/gate + 11 bootstrap）通过，总计 133。新增导入器测试证明错误 blob、缺失/多余来源、metadata 漂移、manifest key/Size/refs 错误均拒绝生成记录；未完成验证的 draft 模式不会伪造 full-GET 标志。
- 首次只读计划：[完整清单](catalog-state/first-cleanup-plan.advisory.json)，1439 文件、3511157520 字节，main base `e58023381e587b2c7ad069120df9c3c8af8e3e7e`，debs tree `cc79b159bef2891feed2d8f317704219aee80e06`。每条带易读原文件名、原路径 base64、Git blob、记录名、SHA256 和 Size。`enabled=false`，没有执行授权。
- 上述初始只读证据自身不代表生产发布；新增真实隔离 E2E 见上节。main 发布仍须合并后独立验收。即使全部 CI 通过，也不能清理首次现有 Deb。
