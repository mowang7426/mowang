# MoWang 越狱软件源

GitHub Pages 托管的 Cydia / Sileo APT 软件源。

## 当前正式发布与管理

正式短源：`https://mowang-repo.pages.dev/`。永久二进制与审计收据在 R2，
`https://pub-f47a5a9b45724684be0f3f006e95e609.r2.dev/repo/` 是 canonical。
短域只开放客户端白名单；snapshots、receipts、zst 等审计读取使用 canonical。

- **管理入口：[main/packages 查找表](packages/README.md)**。删除对应 JSON 后提交，等待
  `Publish complete R2 APT repository` 成功即可下架；R2 对象仍保留，不是物理删除。
- **上传入口：`debs/*.deb`**。新上传先完整校验归档、导入 R2，再 CAS 提交永久 JSON 与
  processed ledger，按永久 catalog 构建发布。Filename 是 `debs/<sha256>.deb`，不要手改。
- **中转自动清理已接通 future-only**：仅在同次可信 publisher job 成功后，从 Actions
  artifact 与 owner-managed immutable S3 收据核对，匹配当前 main、catalog/index 摘要、
  path/blob、大小、SHA256 和 HEAD/S3/public 全 GET 证据，才精确非 force CAS 删除中转。
- 固定 `catalog-state/inbox-cleanup-baseline.json` 排除原 **1439 个 path/blob**。
  首次启用不会清空历史；同路径换新 blob 视为新上传。旧 source JSON 已删除时保留中转，
  不会用 processed ledger 复活已下架记录。head 变化拒绝旧计划，重新发布再派发，不重试陈旧删除。
- 历史整批清理必须另行明确确认精确 plan SHA256 与 base head，随后 workflow_dispatch 同时
  输入 `approved_plan_sha` / `approved_base_head`。默认空值永远只清 future-only；不要自行批准。
- 清理提交只删相符 `debs/*.deb`，不改 packages、ledger 或索引，Git 历史不重写。
  GitHub token bot 提交不触发下一 workflow；本次 job 产出 `inbox-cleanup-evidence` 保留证据。
  空 inbox 仍从永久 catalog 发完整源；下次正常发布重绑新 head，不复用旧收据。
- **R2 物理 GC 始终关闭**，中转清理器没有任何 S3 delete 调用。

以下是旧 Pages/本地构建说明；catalog 存在时永久 JSON 优先，旧 inbox 不是在线包全集。

## 正确目录结构

```text
mowang/
├── CydiaIcon.png
├── Release
├── Packages
├── Packages.gz
├── Packages.bz2
├── Packages.xz
├── Packages.zst
├── build.sh
├── check-debs.sh
├── validate-repo.sh
├── .github/workflows/build.yml
└── debs/
    ├── *.deb
    └── ...
```

**重要：所有 `.deb` 必须放在 `debs/` 目录。** `Packages` 中的 `Filename:` 会自动使用 `debs/<文件名>.deb`，不要手工编辑 `Packages`。

## 发布到 GitHub Pages

1. 将整个目录上传到 GitHub 仓库根目录。
2. GitHub → Settings → Pages → Deploy from a branch → `main` → `/ (root)`。
3. 等 Pages 部署完成后，在 Sileo 添加：

```text
https://mowang7426.github.io/mowang/
```

## 本地重建索引

把新的 `.deb` 放入 `debs/` 后运行：

```bash
./build.sh
```

然后检查：

```bash
./validate-repo.sh
```

脚本会自动生成：

- `Packages`
- `Packages.gz`
- `Packages.bz2`
- `Packages.xz`
- `Packages.zst`（系统有 zstd 时）
- `Release`

其中 `Release` 使用正确的 `Architectures:` 字段，并自动写入索引文件的 MD5/SHA256 校验值。

## GitHub Actions

`.github/workflows/build.yml` 会在 `debs/` 或构建脚本发生变化时自动重建索引并提交生成文件；也支持手动 `Run workflow`。

## 当前架构

```text
iphoneos-arm
iphoneos-arm64
iphoneos-arm64e
```

仓库里的包会按照 `.deb` 控制信息中的 `Architecture` 自动索引。不同架构的同一个 Package/Version 可以同时存在，例如：

```text
com.minis.rainbowkeyboard 1.0.8 iphoneos-arm64
com.minis.rainbowkeyboard 1.0.8 iphoneos-arm64e
```

## 注意

只发布你自己开发或获得授权分发的插件。不要手工修改 `Packages` / `Release`；新增或删除 `.deb` 后重新运行 `build.sh` 即可。
