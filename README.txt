MoWang 源修复 V4

替换：
1. build.sh
2. validate-repo.sh

不要删除 debs/ 中的历史版本来下架：现在请到 main/packages/README.md 查找并删除对应 JSON。
新上传 debs/*.deb 在成功 R2 发布后 future-only 自动清理中转；原1439 path/blob 固定排除，历史批量清理另需确认。
不要删除永久 JSON、processed ledger 或历史排除基线来做磁盘清理；R2 物理 GC 禁用。

build.sh 使用 dpkg-scanpackages -m 保留多版本，允许：
Package 相同 + Version 不同 + Architecture 相同
同时存在，用于 Sileo/Apt 降级。

完全相同 Package/Version/Architecture 的重复文件只警告，不阻止构建。

validate-repo.sh 使用 bash ./check-debs.sh，避免 GitHub Actions 因脚本没有执行权限而出现 exit code 126。
