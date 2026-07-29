# registry-cleaner 开发与构建说明

本文档面向维护脚本源码、修改功能或构建发行包的开发人员。客户使用二进制程序请阅读 `README.md`。

## 本地运行源码

```bash
cd registry-cleaner
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

复制并编辑客户配置：

```bash
cp config.example.json config.json
export REGISTRY_PASSWORD='密码'
python cleaner.py --config config.json --report-tags
```

源码模式常用命令：

```bash
# 单仓库演练/删除
python cleaner.py --config config.json --repository arm64/gitea
python cleaner.py --config config.json --repository arm64/gitea --delete

# 某命名空间演练/删除
python cleaner.py --config config.json --repository-prefix arm64/
python cleaner.py --config config.json --repository-prefix arm64/ --delete
```

默认是 dry-run；只有 `--delete` 才会删除 Manifest。

## 配置

优先使用外部 `config.json`，不要在 `config.py` 中写入客户地址或密码。密码使用 `password_env` 指定的环境变量，默认是 `REGISTRY_PASSWORD`。

`config.py` 仅保存内置默认值。它也包含工作线程、日志和检查点文件名等开发默认设置。

## 构建客户发行包

PyInstaller 仅需安装在构建机，不需要安装到客户机器：

```bash
python3 -m pip install pyinstaller
./build.sh
```

生成 `release/registry-cleaner/`。构建必须在与客户相同的操作系统和 CPU 架构上进行；Linux x86_64、Linux ARM64、macOS 和 Windows 应分别构建。

`build.sh` 使用 PyInstaller 的目录式打包，便于交付 `config.json.example`、README 和 CA 文件。脚本还会生成 `release/SHA256SUMS.txt` 用于校验可执行文件。

## 开发校验

```bash
PYTHONPYCACHEPREFIX=/tmp/registry-cleaner-pycache python3 -m py_compile cleaner.py registry_client.py config.py
python cleaner.py --help
```

## Registry 注意事项

- Registry 需启用 Manifest 删除：`storage.delete.enabled: true`。
- 删除 Manifest 不会立刻释放空间，需在维护窗口运行 Registry GC。
- GC 时必须禁止 push，或把全部共享存储的 Registry 实例设为只读/停止。
- 对自签名证书，开发测试可设 `verify_tls: false`；生产应提供 CA 文件路径。
