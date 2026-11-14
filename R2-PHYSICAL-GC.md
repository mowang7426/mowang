# R2 物理清理：共享协调协议

## 使用范围

删除 packages/*.json 并成功发布后，只有最后一个引用消失才会建立候选；至少 48 小时且至少两个不同成功 publisher 收据确认缺失，才自动物理删除。main GitHub Actions 每天 03:23 UTC（北京时间 11:23）成功发布时复核；失败/排队可能使删除晚于 48 小时。PR/fork 回归无生产 secrets。

首次状态只以当前成功发布引用建立基线，忽略历史 managed-manifest garbage；不扫桶，不回溯旧孤儿。原 1439 个历史 GitHub deb path/blob 没有清空授权，本 PR 不清。外部 owner 不纳管，所有内容键固定为 repo/debs/<sha256>.deb。

## 共享锁及发布顺序

r2_shared_lock.py 的 production-publish.lock 使用已实测的 R2 PUT IfNoneMatch/IfMatch CAS。每个独立 owner 使用随机 token，完整文档绑定 prefix、repository、run、attempt。没有超时抢占/lease renewal。外层 Actions 锁覆盖 import→Git CAS→reload→build→publish→GC；子步骤只在读回完整 owner 文档一致后借锁，嵌套调用不重复申请/提前释放。独立 importer 上传边界和 publisher 也都持同一锁。

异常终止留下锁时 fail closed。人工恢复必须先确认原 job/进程已停止，读回 lock 和成功收据/当前索引并审计；不能在仍存活的持锁者运行时删锁。外部管理员不遵守此协议、任意直接写 S3 的程序不在本协议保证内。

每个重新引用对象发布前必须认证 HEAD、S3 全 GET/hash/Git blob、公开全 GET。缺文件拒绝发布，不用旧 inbox 凑索引。新 importer 创建的对象以不可变 object-claims/<sha>.json 证明归属；owner stamp 本身不会接管旧孤儿。

Release 最后写入并读回校验，managed manifest 成功后写不可变 receipts/<commit>/<run>-<attempt>.json。GC 在同一锁内认证这些字节/owner/hash、当前 Release/Packages/catalog hash 和精确分支 head，再以 CAS 更新 physical-gc-state-v2.json。失败发布不产生缺失确认。每次 DELETE 前重复 head/锁/live 证据 gate，再验内容 owner/version/sha/size；DELETE 不使用 If-Match（实测本 R2 忽略该条件），删除后认证 S3 HEAD 必须 404。

GitHub inbox cleanup 在发布/GC 后才运行。不会放宽 gate 接受任意新 head：若 cleanup 或用户提交先发生，当前 GC 退出；下次 main 定时重新读取、导入、成功发布并产生新收据再清理。Git/S3 不存在绝对跨系统原子性；check 后的新 Git commit 是未发布下一代，不能等同当前线上对象。后续发布必须验证/恢复才能上线。

## 保护窗口、客户端缓存与恢复

共享多记录只删除一个管理记录不会删除内容；重新成功发布引用会取消整个候选事件，再次下架从零计时。旧 schema/foreign state、权限/owner 不符、head 改变或锁丢失均拒绝。生产固定 48h，R2_GC_TEST_NOW 只允许硬编码 catalog-e2e/r2-gc-* 独立 scope。

历史 snapshots 索引保留不等于其引用对象无限保留：最后线上引用下架后保护期至少 48h，超过窗口回滚旧索引可能下载失败。短域/客户端曾配置一年 immutable 缓存，无法远程删除设备已有副本；S3 HEAD404、canonical curl404 表示源端实体/公开新请求不存在，不代表已清除客户端本地副本。

误删/主动恢复：保护期内恢复 JSON 并成功发布可取消候选；如果对象已物理删除，必须从备份或 Git 历史找回同 SHA 的原始 deb 重传，随后恢复管理记录并重新发布。仅恢复 JSON 不恢复字节，publisher 会 fail closed。不要假定 R2 启用版本恢复（现权限无法查询 Versioning）。旧 Git 历史并未因清当前树而缩小。

## 验收

保留原 223 项回归，新增共享锁和真实 publisher 集成回归；独立 catalog-e2e/r2-gc-* 分支使用生产 importer/publisher/receipt/GC 代码验证真实上传、CAS、共享引用、取消/重新计时、失败发布、竞争锁拒绝、真实 DELETE+S3 HEAD404+curl404。测试使用独立前缀，生产对象不作破坏性试验。具体 CI/head 与真实证据由验收报告记录。
