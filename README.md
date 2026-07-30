# registry-cleaner 使用说明

这是 Docker Registry 清理工具的客户使用手册。发行包中的 `registry-cleaner` 是独立可执行程序，客户机器**不需要安装 Python、pip 或 requests**。

开发人员请阅读 `DEVELOPMENT.md`。

## 发行包内容

```text
registry-cleaner/
├── registry-cleaner          # 可执行程序
├── config.json.example       # 客户配置模板
└── README.md
```

第一次使用时，在该目录执行：

```bash
cp config.json.example config.json
```

然后使用文本编辑器填写 `config.json`。

## 配置

示例：

```json
{
  "registry_url": "https://registry.customer.example",
  "username": "registry-user",
  "password_env": "REGISTRY_PASSWORD",
  "verify_tls": "/opt/registry-cleaner/registry-ca.crt",
  "keep_last": 3,
  "fallback_to_last_modified": true,
  "skip_repository_prefixes": ["tools/", "base/", "cicd/"],
  "skip_repositories": [],
  "protected_tag_patterns": [],
  "repository_workers": 2,
  "tag_workers": 12
}
```

| 参数 | 作用 | 建议 |
| --- | --- | --- |
| `registry_url` | Docker Registry 访问地址。 | 例如 `https://192.168.202.4`。 |
| `username` | Registry 登录用户名。 | 填实际账号。 |
| `password_env` | 保存密码的环境变量名。 | 默认 `REGISTRY_PASSWORD`。 |
| `verify_tls` | TLS 证书校验方式。 | 填 CA 文件路径最安全；自签名证书临时可填 `false`。 |
| `keep_last` | 每个仓库保留的最新 tag 数量。 | `3` 表示每个仓库各保留 3 个。 |
| `fallback_to_last_modified` | `created` 缺失时，是否用 `Last-Modified` 排序。 | 建议 `true`。 |
| `skip_repository_prefixes` | 跳过指定前缀下所有仓库。 | `tools/` 会跳过 `tools/*`。 |
| `skip_repositories` | 跳过指定完整仓库名。 | 例如 `["arm64/gitea"]`。 |
| `protected_tag_patterns` | 永远不删除的 tag 或通配符。 | 如 `["latest", "prod-*"]`；严格保留 3 个则用 `[]`。 |
| `repository_workers` | 同时处理的仓库数。 | 默认 `2`；压力大时设为 `1`。 |
| `tag_workers` | 单仓库并发读取元数据数。 | 默认 `12`；压力大时降至 `4–8`。 |

总并发约等于 `repository_workers × tag_workers`。

## 设置密码

密码不能写入 `config.json`。在运行前设置环境变量：

```bash
export REGISTRY_PASSWORD='你的密码'
```

请使用键盘输入的英文半角单引号 `'`，不要使用智能引号 `‘ ’`。

## 查询 tag 最多的仓库

此操作只读取 tag 列表，不会删除任何数据：

统计仓库中任何空间下的`所有镜像`:
```bash
./registry-cleaner --config config.json --report-tags
```

生成 `registry-tag-counts.csv`，按 tag 数量从高到低排序：

```bash
head -n 21 registry-tag-counts.csv
```

命名空间下所有 `arm64/`：

```bash
./registry-cleaner --config config.json --repository-prefix arm64/ --report-tags
```

只统计单个 `arm64/gitea`：

```bash
./registry-cleaner --config config.json --repository arm64/gitea --report-tags
```

## 清理操作

### 清理单个镜像的tag

先演练，默认不会删除：

```bash
./registry-cleaner --config config.json --repository arm64/gitea
```

查看待删除记录：

```bash
grep would_delete registry-cleaner-actions.csv | tail -n 30
```

确认无误后才删除：

```bash
./registry-cleaner --config config.json --repository arm64/gitea --delete
```

### 清理 arm64 下的全部镜像的tag

每个 `arm64/*` 镜像会独立保留最新 `keep_last` 个 tag。

```bash
# 先演练
./registry-cleaner --config config.json --repository-prefix arm64/

# 确认后实际删除
./registry-cleaner --config config.json --repository-prefix arm64/ --delete
```

### 清理所有命名空间下的镜像tag

```bash
# 先演练
./registry-cleaner --config config.json

# 确认后实际删除
./registry-cleaner --config config.json --delete
```

## 保留规则

工具优先按镜像 config 的 `created` 时间排序；该时间缺失时可使用 Registry 返回的 `Last-Modified`。无法获得可靠时间的 tag 会保留，避免误删。

Registry 只能按 Manifest digest 删除，不能单独删除 tag。若多个 tag 共用一个 digest，且其中任一 tag 需要保留，工具会保留这个 digest 的所有 tag。因此少数仓库实际 tag 数可能超过 `keep_last`，这是安全设计。

## 日志、审计和继续执行

运行日志、CSV 和检查点会写入 `config.json` 所在目录（未指定 `--config` 时写入当前工作目录）。

| 文件 | 作用 |
| --- | --- |
| `cleaner.log` | 详细运行日志。 |
| `registry-cleaner-actions.csv` | 每个 tag 的操作审计记录。 |
| `checkpoint.json` | 已完成仓库的检查点。 |
| `registry-tag-counts.csv` | tag 数量统计结果（`--report-tags` 与预清理/删除前均会生成）。 |

运行中断后可继续：

```bash
./registry-cleaner --config config.json --repository-prefix arm64/ --delete --resume
```

如果刚修改过配置或保留规则，不要使用 `--resume`，以免旧检查点跳过需要重新评估的仓库。

## 删除后的 GC

删除 Manifest 不会立即释放磁盘空间。需在维护窗口执行 Registry 的 GC。

GC 前必须停止 push，或把 Registry 设为只读；如果多个 Registry 实例共享存储，全部实例都必须停止写入。否则新上传镜像的 layer 可能被 GC 误删。

Registry 必须开启删除功能：

```yaml
storage:
  delete:
    enabled: true
```

典型 GC 命令：

```bash
docker exec <registry-container> registry garbage-collect /etc/docker/registry/config.yml
```
