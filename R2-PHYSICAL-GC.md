# R2物理清理：受保护实现（未启用）

此PR必须保持draft，不能合并冒充上线。生产publish-r2.py未接入新引擎，physical_gc_enabled=false，旧1439个历史path/blob及生产对象不动。仅新增脚本/测试，原future-only GitHub inbox清理不变。

## 实现与测试

r2-physical-gc.py首次只记录当前成功快照引用的managed对象，忽略旧garbage。随后最后引用消失才建立事件，至少48小时、至少两次不同成功receipt缺失确认；共享引用保留，恢复取消并重置事件，时钟倒退拒绝。限定不可变debs/<sha256>.deb，验证owner/version/sha/size，每次DELETE前重复gate，删除后S3 HEAD验404。唯一包删除函数硬封锁repo/，只允许catalog-e2e/r2-gc-*测试前缀和测试owner；无生产enable参数、无扫桶或历史清空。

15项新增回归纳入原工作流，原208项测试全部保留，本机共223项通过。

## 真实R2阻断证据

两次全新隔离前缀catalog-e2e/r2-gc-20261114-a1/与a2/，错误If-Match的DeleteObject返回204，随后S3 HEAD404，未返回预期412。SDK明确将IfMatch序列化为HTTP If-Match头。只删除隔离生成随机内容对象，无生产DELETE。a2报告success=false、conditional_delete_supported=false。共享记录、单次快照不足、48h双快照、恢复取消在引擎试验通过；这不是完整APT publisher E2E，后续owner/restore-race/public缓存全链验收未完成，不得冒称通过。GetBucketVersioning被当前凭证AccessDenied拒绝，不能假定有可恢复版本。

## 必须完成才可移除生产封锁

1. import-catalog上传、所有其他写入器、publisher、GC共享同一不可抢占S3 CAS lease。现import上传不持有publisher锁，单靠DELETE前check main或未经验证IfMatch不安全。
2. 重新引用的对象发布前必须verify/reupload；原deb若已被future-only GitHub cleanup删除，恢复JSON不等于恢复文件，须补传原deb，缺对象拒绝发布。
3. GC仅接受可信main工作流成功publisher的不可变owner收据，再校验live Release/Packages/catalog/manifest/hash；GitHub cleanup合法head变化必须绑定精确树等价证据。不能接收任意dispatch prefix/receipt。
4. future-only状态仅在全成功发布后CAS持久化，旧manifest garbage不继承。至少48h与旧generation保留窗口结合。Git提交和S3没有跨系统原子性，必须明确新head未发布窗口，并用共同锁与再次verify/reupload协议收敛，不能许诺绝对原子。
5. 完整生产代码隔离E2E验证共享两记录、最后引用、失败发布、外部owner、恢复、pending同hash、新head竞争、双workflow、锁丢失、权限失败、S3/public404与缓存审计。
6. 成熟后接入可信main GitHub Actions schedule，已有每天03:23 UTC发布；不能用iOS定时替代。PR与任意dispatch不得取得生产GC能力。

## 中文使用语义

目标：删除packages JSON → 成功publish下架 → 最后引用才建候选 → 至少48h并多轮可信成功快照 → 自动GC。删一条共享记录不删实体；延迟内恢复取消事件；基线前已下架/外部/桶里其他对象不纳管。

当前真实行为仍是JSON删除后发布下架、R2保留，没有自动物理删除上线。原1439 GitHub deb历史树未动，可从现存debs或Git历史恢复；将来source被cleanup删除时应找回备份原deb重新上传。删除当前树文件不保证Git历史占用减少。
