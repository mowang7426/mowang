#!/usr/bin/env bash
# 检查 debs/ 目录里所有 .deb 是否都能被正常解析
# 用法：./check-debs.sh   （有坏包时返回非 0 并列出文件名）
set -uo pipefail
cd "$(dirname "$0")"
broken=0
for f in debs/*.deb; do
  [ -e "$f" ] || continue
  # 指定字段会触发严格解析；不指定字段仅原样展示 control，可能漏报。
  if ! dpkg-deb --field "$f" Package Version Architecture >/dev/null 2>&1; then
    echo "[坏包] $f"
    broken=1
  fi
done
# dpkg-deb -I 只展示 control 内容，不保证字段/续行语法有效。
# 使用构建时相同的严格解析器，禁止不合法元数据绕过检查。
if [ "$broken" -eq 0 ]; then
  if ! command -v dpkg-scanpackages >/dev/null 2>&1; then
    echo "❌ 缺少依赖：dpkg-scanpackages（dpkg-dev）"
    exit 1
  fi
  if ! dpkg-scanpackages -m debs >/dev/null; then
    echo "[坏包] control 字段语法检查失败（详见上方具体文件和行号）"
    broken=1
  fi
fi
if [ "$broken" -eq 0 ]; then
  echo "全部 $(ls debs/*.deb 2>/dev/null | wc -l | tr -d ' ') 个 deb 均通过归档/control 字段检查 ✔"
else
  echo "存在坏包：请重新打包后再上传，否则 GitHub Actions 构建会失败。"
fi
exit $broken
