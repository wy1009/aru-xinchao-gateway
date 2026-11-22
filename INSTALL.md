# 安装与测试

本项目针对单用户 Linux 网关。心潮念版本固定为 `444e7db2d138ad7ff23cafd774a5c0d3eb37aae5`。以下命令从本网关源码根目录运行，需要 Python 3.12、Node.js 22 或更高版本、Git、curl、jq 和 uv。

```sh
git clone https://github.com/tianyupaipai-cmd/xinchao-nian.git ../xinchao-nian
git -C ../xinchao-nian checkout --detach 444e7db2d138ad7ff23cafd774a5c0d3eb37aae5
./scripts/prepare-upstream.sh ../xinchao-nian
export NIAN_UPSTREAM_DIR="$(cd ../xinchao-nian && pwd)"
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --require-hashes -r "$NIAN_UPSTREAM_DIR/ombre-brain/requirements.lock.txt"
ARU_COLLABORATOR_ID=00000000-0000-4000-8000-000000000001 PYTHONPATH="$NIAN_UPSTREAM_DIR/ombre-brain/src" .venv/bin/python -B -m unittest discover -v
node aru_wake/test.mjs
```

## 连接网关

1. 按固定版本的上游说明运行心潮和 OB。原生部署中，心潮监听本机 18110，OB 监听本机 18001。网关通过 `NIAN_MIND_URL` 和 `NIAN_OB_URL` 访问它们。心潮的 `SERVICE_TOKEN` 与网关的 `NIAN_MIND_TOKEN` 使用同一值，OB 和网关另共享 `NIAN_INTERNAL_TOKEN`。
2. 为网关创建私有环境文件。配置 `XINCHAO_CHAT_API_KEY`、`NIAN_MIND_TOKEN`、`NIAN_INTERNAL_TOKEN`、`NIAN_CLIENT_SHA256`、`NIAN_UPSTREAM_DIR` 和 `NIAN_QUEUE_DB`。客户端使用单独随机生成的 Bearer 令牌，`NIAN_CLIENT_SHA256` 填该令牌 UTF-8 字节的 SHA-256 小写十六进制摘要。上游聊天地址和模型名可通过 `NIAN_CHAT_URL` 和 `NIAN_MODEL` 修改。使用 Aru 备份作为记忆源时设 `NIAN_CHAT_MEMORY_IMPORT=0`。
3. 给网关进程创建专用可写的队列目录。在加载上述变量的环境中，从本项目根目录运行 `.venv/bin/uvicorn gateway:app --host 127.0.0.1 --port 18111 --no-access-log`。生产可用 `systemd/nian-gateway.service`，按实际目录、服务账号和虚拟环境路径调整。HTTPS 反向代理将 `/nian/` 前缀剥离后送往该端口，客户端填 `https://你的域名/nian/v1`。
4. 将 `ARU_COLLABORATOR_NAME` 设为实际协作者名，网关和通知桥的环境都要设置这个变量，普通聊天和主动通知的状态标题会使用同一名字。备份导入另需 `ARU_COLLABORATOR_ID`，其值是原备份中 `collaborator.id`。请在网关、OB 和导入服务上使用同一组协作者设置。已有同步数据库绑定一位协作者，切换身份时使用独立的状态目录与 OB 库。

## 可选组件

- **备份上传页**使用 `aru_portal:app`。按 `systemd/aru-backup-*` 的服务路径将 `aru_*.py`、`aru_portal.html`、`wake_import.py` 和 `prompt_audit.py` 放入收件服务目录。创建独立的服务账号及上传密码，`INBOX_DIGEST` 是该密码的 SHA-256 摘要。常驻服务读取 `aru-backup-inbox.env`。在导入服务的 systemd drop-in 中加载含协作者设置的环境文件。导入和 AI 补全服务通过 `LoadCredential=gateway-internal` 取得与 OB 的 `NIAN_INTERNAL_TOKEN` 相同的凭据。反向代理向本机 19148 转发完整的 `/aru-backup` 路径。首次使用可先运行 `aru_backup.py 备份文件` 预览。
- **手机通知桥**需要已经与手机配对的 Aru Host v0.39.2 和手机导出的发送方 JSON。将 `aru_wake/` 安装到单元指定的目录，再把 `prompt_audit.py` 复制到该目录。发送方配置保存在 `/etc/nian-aru-bridge/sender.json`，仅允许管理员和桥服务账号读取。上传页的 `/aru-backup/xinchao` 是受认证的暂存入口；管理员核对其端点、提交令牌摘要及加密密钥指纹与 Host 登记一致后，将配置安装到上述路径。设置 `ARU_PUBLIC_ORIGIN` 为导出配置中端点的 HTTPS origin（例如 `https://gateway.example.org`），在通知桥和上传页使用同一值。桥使用本机 Host 的 8787 端口。
- **通过网关使用心潮 MCP** 时，在心潮服务进程的环境中设置 `XINCHAO_MCP_STATE_IN_SYSTEM=1` 并重启心潮。工具直接返回自己的结果，状态由网关按对话窗口加入 system。默认值沿用上游为独立 MCP 客户端提供的工具尾注。
- **桥的心潮端**设置 `BRIDGE_ENABLED=true` 和 `BRIDGE_MACHINE_TOKEN`。运行时设置 `XINCHAO_BRIDGE_BASE_URL=http://127.0.0.1:18110`、相同的 `XINCHAO_BRIDGE_MACHINE_TOKEN`、指向 Node 的 `XINCHAO_BRIDGE_INJECTOR_EXECUTABLE`、参数数组 `XINCHAO_BRIDGE_INJECTOR_ARGS_JSON=["/opt/xinchao-nian/aru-wake/adapter.mjs"]`，以及 `XINCHAO_BRIDGE_INJECT_TIMEOUT_MS=30000`。运行目录和 socket 见 `aru_wake/` 的 systemd 文件。需要自身信号时，两端分别设 `BRIDGE_SELF_SIGNALS=true`、`XINCHAO_BRIDGE_ACCEPT_SELF_SIGNALS=true`。
- **七天日志**保存发给模型的对话、可见回复和手机收件回执。两类数据分别位于 `/var/lib/nian-gw/prompt-audit` 与 `/var/lib/nian-aru-bridge/prompt-audit`。让对应服务账号拥有目录和数据库，收件页服务组只读。每个目录内初始化 SQLite 文件 `records.db`，然后启用 `nian-audit-expire@nian-gw.timer`、`nian-audit-expire@nian-aru-bridge.timer` 并为上传服务安装 `aru-inbox-audit.conf`。查看路径为 `/aru-backup/xinchao-log`。

```sql
CREATE TABLE IF NOT EXISTS audit (id TEXT PRIMARY KEY, at REAL NOT NULL, status TEXT NOT NULL, entries TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS audit_by_time ON audit(at);
```
