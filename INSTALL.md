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

若只希望通过主动唤醒收到心潮消息，在网关环境文件设 `NIAN_INJECT_MIND_STATE=heartbeat-only`，再重启网关。正常聊天只上报到场心跳。此设置也适用于新会话。默认值 `1` 会按官方节奏附加状态，`0` 关闭整个聊天前钩子。

## 可选组件

只用网关和 OB 时，在网关环境中设 `NIAN_MIND_ENABLED=0` 并重启网关，再执行 `systemctl disable --now nian-aru-bridge.service nian-mind.service`。网关会跳过心潮心跳、状态注入、小屋回传和情绪自评。聊天转发、用量账本和 Aru 备份导入可独立运行。Leto 也使用 `nian-aru-receipt.socket`，启用 Leto 时应保留该 socket。恢复时先执行 `systemctl enable --now nian-mind.service nian-aru-bridge.service`，再把网关开关设为 `1` 并重启网关。

暂停 OB 时，可在收件服务环境中设 `ARU_OB_SYNC_ENABLED=0` 并重启 `aru-backup-inbox`，使上传和补全入口返回 HTTP 503。日志和手机绑定仍可使用。待在途导入完成后，停用 `aru-backup-import.path`、`aru-backup-enrich.path`、`nian-ob-web.service` 和 `nian-ob.service`。

- **备份上传页**使用 `aru_portal:app`。按 `systemd/aru-backup-*` 的服务路径将 `aru_*.py`、`aru_portal.html`、`wake_import.py` 和 `prompt_audit.py` 放入收件服务目录。创建独立的服务账号及上传密码，`INBOX_DIGEST` 是该密码的 SHA-256 摘要。常驻服务读取 `aru-backup-inbox.env`。在导入服务的 systemd drop-in 中加载含协作者设置的环境文件。导入和 AI 补全服务通过 `LoadCredential=gateway-internal` 取得与 OB 的 `NIAN_INTERNAL_TOKEN` 相同的凭据。反向代理向本机 19148 转发完整的 `/aru-backup` 路径。首次使用可先运行 `aru_backup.py 备份文件` 预览。
- **手机通知桥**需要已经与手机配对的 Aru Host v0.39.2 和手机导出的发送方 JSON。将 `aru_wake/` 安装到单元指定的目录，再把 `prompt_audit.py` 复制到该目录。发送方配置保存在 `/etc/nian-aru-bridge/sender.json`，仅允许管理员和桥服务账号读取。上传页的 `/aru-backup/xinchao` 是受认证的暂存入口；管理员核对其端点、提交令牌摘要及加密密钥指纹与 Host 登记一致后，将配置安装到上述路径。设置 `ARU_PUBLIC_ORIGIN` 为导出配置中端点的 HTTPS origin（例如 `https://gateway.example.org`），在通知桥和上传页使用同一值。桥使用本机 Host 的 8787 端口。
- **通过网关使用心潮 MCP** 时，在心潮服务进程的环境中设置 `XINCHAO_MCP_STATE_IN_SYSTEM=1` 并重启心潮。工具直接返回自己的结果，状态由网关按对话窗口加入 system。默认值沿用上游为独立 MCP 客户端提供的工具尾注。
- **桥的心潮端**设置 `BRIDGE_ENABLED=true` 和 `BRIDGE_MACHINE_TOKEN`。运行时设置 `XINCHAO_BRIDGE_BASE_URL=http://127.0.0.1:18110`、相同的 `XINCHAO_BRIDGE_MACHINE_TOKEN`、指向 Node 的 `XINCHAO_BRIDGE_INJECTOR_EXECUTABLE`、参数数组 `XINCHAO_BRIDGE_INJECTOR_ARGS_JSON=["/opt/xinchao-nian/aru-wake/adapter.mjs"]`，以及 `XINCHAO_BRIDGE_INJECT_TIMEOUT_MS=30000`。运行目录和 socket 见 `aru_wake/` 的 systemd 文件。需要自身信号时，两端分别设 `BRIDGE_SELF_SIGNALS=true`、`XINCHAO_BRIDGE_ACCEPT_SELF_SIGNALS=true`。
- **七天日志**保存发给模型的对话、可见回复和手机收件回执。两类数据分别位于 `/var/lib/nian-gw/prompt-audit` 与 `/var/lib/nian-aru-bridge/prompt-audit`。让对应服务账号拥有目录和数据库，收件页服务组只读。每个目录内初始化 SQLite 文件 `records.db`，然后启用 `nian-audit-expire@nian-gw.timer`、`nian-audit-expire@nian-aru-bridge.timer` 并为上传服务安装 `aru-inbox-audit.conf`。查看路径为 `/aru-backup/xinchao-log`。将 `usage_ledger.py` 同时放入网关目录和收件页目录。网关会在自己的 `prompt-audit` 目录生成独立的 `usage.sqlite` 账本。给目录设置收件页服务组和 setgid 位，新账本以 0640 创建供收件页读取。账本仅存调用标识、时间、用途、模型和用量费用，独立于聊天正文的七天清理。

```sql
CREATE TABLE IF NOT EXISTS audit (id TEXT PRIMARY KEY, at REAL NOT NULL, status TEXT NOT NULL, entries TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS audit_by_time ON audit(at);
```

### 将 Leto 的群聊唤醒转给同一个 Aru 会话

本组件使用 [Leto 官方唤醒桥](https://github.com/oliscatt/leto-wake-bridge/tree/54fbcf78851b3c45d6880af3eb83d73b6a1122b1) 的 SSE 订阅、待办补查和投递队列。安装前先完成上面的 Aru Host 配对、发送方配置和回执 socket，运行 `node --test aru_wake/leto.test.mjs` 验证适配器。

1. 在 `/opt/leto-wake-bridge/` 下安装本仓库的 `aru_wake/leto.mjs`、`transport.mjs` 和 `aru/wake-send.mjs`，保持这三个文件的相对路径。将上述官方版本的 `bridge.mjs` 和 `LICENSE` 放入该目录下的 `upstream/`。
2. 创建同名系统用户和组 `leto-wake-bridge`。在仅 root 可读的 `/etc/leto-wake-bridge.env` 中设置 `LETO_MACHINE_TOKEN`、`LETO_BASE_URL=https://leto.sarl` 和与发送方配置一致的 `ARU_PUBLIC_ORIGIN`。桥会在每次投递时读取当前手机绑定。
3. 将 `aru_wake/leto-wake-bridge.service` 安装到 `/etc/systemd/system/`，将 `aru_wake/leto-aru-bridge.tmpfiles.conf` 安装到 `/etc/tmpfiles.d/`。运行 `systemctl daemon-reload` 和 `systemctl enable --now leto-wake-bridge.service`。单元使用 systemd 254+ 的重启退避。
4. 在手机 Aru 给对应协作者启用 HTTP MCP `https://leto.sarl/mcp`，请求头为 `Authorization: Bearer <Leto 机器令牌>`。在已加入的 Leto 群里 @ 该协作者即可联测。桥日志里的“已投递”表示手机回了收件回执，实际群回复可在 Leto 查看。

## Aru 完整备份

在上传服务的应用目录部署 `aru_archive_upload.py` 和 `aru_archive_upload.html`。访问 `/aru-backup/archive/`，使用原站的 Aru 认证上传完整备份。页面支持 4 MiB 分块校验和续传，文件保存在该服务私有状态目录的 `archive-uploads/<清单摘要>/archive.polarisarchive` 中，等待核对。

## 顾守的 Hindsight 观察召回

将 `hindsight_gateway.py` 随网关部署，把 `systemd/96-hindsight.conf` 安装到 `nian-gateway.service.d/`，重载 systemd 并重启网关。服务凭据来自本机 Hindsight 的 `/etc/gushou-hindsight/local-key`。顾守入口以最新一条用户消息查询 `gushou` 库的观察，沿用 OpenClaw 的提示文本和 1,024 token 预算。召回简报会显示实际注入的观察与条数。停用旧接入时，同时停用 Serein 服务、同步定时器及其整理任务。

## 旧版 Serein 试用配置

Serein 上游仓库为 `https://github.com/Yinglianchun/Serein`，使用提交 `fdab5a8af8c966572c47faabed18223079bc91c4`。
`nian-gateway` 从 `serein_gateway.py` 调用本机的 `gushou-serein`（18118）和
`gushou-serein-bridge`（18119）。`systemd/95-serein.conf` 在顾守的网关上启用接入。
两个服务的内部认证使用 systemd 凭据，Aru 客户端和备份页沿用现有认证。

在服务器的 `/etc/gushou-serein/web.env` 设置 `SEREIN_PUBLIC_ORIGIN=https://你的域名`。管理后台的「记忆」页在该站点的 `/__serein/#memory`，与备份页共用 Aru 登录。
官方 Web 前端以 `SEREIN_BASE_PATH=/__serein/` 构建。将 `aru_serein_web_auth.mjs` 放入 Web 的 `server/` 目录，把 `server/gateway.mjs` 中 `requireWebAuth` 的导入路径从 `./webAuth.mjs` 改为 `./aru_serein_web_auth.mjs`，即可沿用备份页的认证。Caddy 将 `/__serein/*` 原样转给
`gushou-serein-web.service`（本机 18217）。备份密码更换时一并重启该服务，以重新加载凭据。

顾守以手动上传的完整 `.polarisarchive` 为原话来源。`gushou-serein-sync.timer` 检查已完成的上传，按会话和备份消息 ID 校正新增、修改、删除及排除，只导入顾守获准会话的聊天原文。服务器上的 `/etc/gushou-serein/.backup-only` 使桥接服务拒绝实时原话，启用备份校正。网关设置 `NIAN_SEREIN_LIVE_CAPTURE=0`，普通聊天仍可召回已整理的事件。

原话正文被确认改写后，下一轮会针对新版重新归线、判断和撰写事件，即便先前曾跳过该句、当前只有一条待处理的用户消息。整理器会看到修订消息的编号和当前有效的相邻原话。修订原因只采用双方在对话里明确说过的内容。首次导入某条备份消息时，若 Aru 的修订记录能证实存在不同的已完成正文，也会触发复核。已处理的消息重复上传相同正文时沿用现有结果。可在队列 `status` 的 `pending_edit_reviews` 查看待复核的原话数量。

云端 Codex 每天北京时间 03:00 和 15:00 核对最新完整备份。只有发现新包，才领取 `/opt/gushou-serein/serein-agent-queue.py` 中的备份归线、切分和事件写作任务；成功完成后，在 `background_state.aru_codex_finished_snapshot_v1` 记录该包的时间与上传标识，失败保留给下次重试。`/etc/gushou-serein/.codex-curation` 标记保留 Codex 整理方式。管理页「记忆 → 事件」可以查看结果并展开来源原文。「沉淀」使用 Serein 自己的 Event 到 Scene 流程。

实例专属的事件写作约定可以保存在服务器的 `/etc/gushou-serein/writer-wording.txt`（UTF-8）。整理器优先使用这份文件，未配置时使用内置写作规则。

每次聊天只用主人刚发的这一条消息查询一次 Serein Hook，检索时去掉客户端附加的发送时间。网关按 Hook 的排序注入通过来源校验的记忆卡。每张卡最多附一轮已核对的完整历史原话，以「主人」「顾守」标记。近期已显示的完整对话和多张卡共用的同一轮原话会去重。

本实例在 Serein 包目录以 `-p1` 应用 `scripts/serein-chitchat-recall.patch`。闲聊、当下情境、亲近和技术闲聊四类消息继续参与候选检索与重排。升级 Serein 时需核对此处路由。登录 `/aru-backup/xinchao-log?feed=recall` 可查看最近十次请求的实际记忆注入，每次最多显示十条聊天消息，正文默认折叠。

服务状态可在服务器上查看。

```sh
systemctl status nian-gateway gushou-serein gushou-serein-bridge gushou-serein-sync.timer gushou-serein-web
journalctl -u gushou-serein-sync.service -n 12
```
