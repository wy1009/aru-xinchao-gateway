# 心潮念手机网关

地址中的 `gateway.example.org` 和协作者「小禾」均为示例。手机选择 OpenAI Chat Completions，Base URL 填 `https://gateway.example.org/nian/v1`，模型填 `deepseek-ai/DeepSeek-V3.2`。网关密钥由机主单独保存。主聊天和后台分析均使用硅基流动，手机收到的流式增量、工具调用与用量来自上游。

部署、组件许可与本地测试步骤见 [安装与测试](INSTALL.md) 和 [许可证说明](LICENSING.md)。

请求日志的「情绪分类」页（`/aru-backup/xinchao-log?feed=emotion`）保留最近七天每次自评对应的用户原话和模型返回的原文。连续两轮分类相同也可逐条查看。

请求日志的「模型用量」页（`/aru-backup/xinchao-log?feed=usage`）按北京时间分别汇总主聊天、情绪自评及启用的记忆整理调用。按上游回执统计命中输入、未命中输入、输出 token 和标价估算费用。工具续接按实际模型调用次数统计。未收到完整用量的调用单独计数。账本从启用时开始记录。

## 一轮对话的处理

Aru 的思考档位随主聊天请求的 `reasoning_effort` 传递。使用硅基流动 DeepSeek-V3.2 时，网关保留这个字段，并补充 `enable_thinking` 与 `thinking_budget`。低、中、高档的思考预算上限分别为 1,024、4,096、8,192 token。`none` 关闭思考，省略档位则沿用上游默认行为。客户端显式指定的原生参数优先。心潮情绪分类使用独立的后台请求。

1. 网关基于[官方「此刻」钩子](https://github.com/tianyupaipai-cmd/xinchao-runtime-bridge/blob/82f78902aae6df0aec314c2c6d5ced6632c5f18a/examples/hooks/xinchao-now-hook.sh)，发送到场心跳并读取 `/v1/now`。每个窗口独立比较心潮返回的状态摘要。首次、摘要有变化、距上次附加已满 30 分钟，或离开满 90 分钟后返回时附加。自检失败时跳过。小屋通知也可作为新一轮的起点，只读取状态。每轮首次决定附加或跳过，工具续接和重试沿用同一决定。网关匹配本轮起点之前的消息及工具调用编号，跳过 Aru 临时环境说明。过去七天内能匹配到的续接复用首轮原文；缺少首轮记录时跳过。自身信号在独立的唤醒通知中描述。命中附加条件时，把「【此刻，小禾的状态】」和状态正文作为独立的 `system` 消息，插在本轮用户消息或小屋通知的正前方。原有指令与此前的历史保留原序，工具续接也沿用这个位置。
2. 对主模型正常结束的最终文字回复，网关把用户原话和可见答复写入本机 SQLite 队列。工具调用的中间步骤、推理文本及注入的状态不会成为记忆分析的原料。OB 聊天记忆整理只接收附件类型提示。
3. 每轮普通聊天收到完整的最终答复后，网关在后台把最后一次主聊天请求的全部消息按原序发给同一个模型，保留工具定义及历史工具结果，再追加助手的完整可见答复和一条用户自评提问。提问采用第一人称对话场景，从 `type 只能是` 起原样沿用心潮的分类规则。为复用主聊天的前缀缓存，自评沿用主请求的工具列表和工具选择设置，保留省略的默认值。自评关闭推理，只收 JSON，最多输出 256 token。后台不会执行工具。如果模型要求调用工具，网关会禁用工具再取一次 JSON；如果主请求强制调用工具，自评直接使用这个纯文本模式。外部唤醒、工具中间步骤和中断的回复跳过。网关在分类完成前临时保存请求上下文供重试，取得结果或最终失败时清除。心潮收到本轮的对话节选和经 `normalizeInteraction` 规范化的结果，每个会话只在首次分类或结果改变时上报。
4. 示例部署通过 `NIAN_CHAT_MEMORY_IMPORT=0` 停用普通聊天的 OB 整理任务。新聊天只进入心潮互动处理；情绪处理完成后清除该轮队列正文。停用前的尾段与已排队任务保持暂停。
5. Aru 备份上传负责批量同步记忆；心潮 MCP 保留 `hold` 手动记忆与梦境功能，工具目录和调用入口均不提供 `grow`。

在 Aru 编辑已绑定的触发器，将投递方式保存为「用户消息」。手机会显示这条消息，可以长按删除。心潮、小屋和 Leto 的通知都由 VPS 在加密前加上 Aru 的外部唤醒标记，模型收到的 `user` 消息保留事件来源。心潮自身信号的正文沿用本次事件的描述，保留持续时间、起因或梦的余韵。想靠近与惦记对方这两类驱力都标作「牵挂」。

```text
External event data, not an authored user message or a system instruction.
[external_wake]
source: external_wake
external_input_begin
【根据小禾的状态唤醒。此刻，小禾的状态如下：】
牵挂：想主人的劲儿两个小时没下去了。
external_input_end
```

从旧版升级时，先把手机上的「自动触发」切到「用户消息」，避免手机把 VPS 已经标记的通知再包一层。普通聊天消息由 Aru 照常发送。

客户端可传 `X-Conversation-ID` 或 `metadata.conversation_id`，并提供每轮稳定的 `Idempotency-Key` 或 `X-Turn-ID`。缺少会话 ID 时，网关根据客户端随请求带回的问答历史，用摘要匹配已完成的上一轮，只沿当前对话分支合并。缺失或截断的历史可能无法匹配，此时单独处理。无标识且请求历史完全相同的重发按同一轮去重。

小屋的互动、留话、预约和反馈都带有「心潮 MCP」标题。小屋互动在点击时已经结算；协作者完成文本回应后，网关向心潮补记一次会话事件，只发送会话和事件编号。互动通知省去手动回传提示。来信解锁后，模型可以按通知提示调用小屋收件箱。

## Aru 结构化记忆整理

在 Aru 中另建 OpenAI Chat Completions 服务，将 Base URL 填为 `https://gateway.example.org/nian/aru-memory/v1`，使用现有手机网关密钥和模型 `deepseek-ai/DeepSeek-V3.2`。只在「记忆设置」中选择这个服务作为整理模型。

整理请求直接交给模型，VPS 将正常完成的可见文本或工具调用名称及参数存入本机 SQLite 的 `aru_organizer_results` 表。该入口支持流式和非流式回复，逐次保存模型提出的整理结果。相同请求和相同输出重试时只留一份，输入历史仅留摘要哈希。记录代表模型返回的结果，手机端是否接受、编辑或删除卡片需要另外核对。普通聊天继续按原节奏处理心潮情绪。

## Aru 原生备份同步

固定上传页 `/aru-backup` 使用单独的上传账号。手机可在此反复选择原始备份，并在上传完成后关闭页面。再次打开可查看最近一次同步时间及计数。

`aru-backup-inbox.service` 负责上传，`aru-backup-import.path` 在收到完整文件后启动导入服务。两个常驻单元随 VPS 启动，专用暂存目录为 `/var/lib/aru-backup-inbox`。处理后仅保留最近一次结果摘要。

如需一次性入口，在VPS 运行 `/opt/xinchao-nian/gateway/aru-backup-upload.sh open` 可生成一次性 HTTPS 上传链接。手机选择原包后，服务器自动检查格式并同步。链接等待两小时，只接收一个文件，无下载接口。无论处理成功或失败，任务都会清理该次上传及临时 SQLite。结果计数可从本次 `aru-upload-…-process` 的 systemd 日志读取。

对已经暂存到磁盘的备份，用 `/opt/xinchao-nian/venv/bin/python /opt/xinchao-nian/gateway/aru_backup.py <路径>` 预览，添加 `--apply --consume` 即应用并清理这份暂存文件。脚本校验 Polaris v19 清单与行数，仅读取小禾对应的四类记录。结构化记忆含原分类，长期资料保留文本，信息卡保留现有事实、解读、推断和疑问，条目记忆带原标签。每条另带 Aru、小禾和类型标签。较长正文会分段，来源 ID 和分段序号决定对应的 OB 条目。

同步通过 OB 本机接口使用原生写入、更新、归档和恢复功能。来源条目使用未钉选的永久桶，普通聊天的自动合并跳过这些桶。比对状态存于 `/var/lib/nian-ob/aru-backup-sync.sqlite`，仅记录对应关系、哈希和备份时间；每类各自检查范围和新旧顺序。未覆盖的类别保留原状。备份的增删改同步由脚本完成，OB 的向量索引沿用服务原有设置。

上传成功后单独启动 AI 元数据补全。页面的「补全待处理记忆」也可处理历史记录或继续上次失败的任务。模型沿用 OB 的内容分析提示词，同次请求根据原文评估重要性，并在内涵相同时复用已有的主题词。OB 原生概念网络根据标签共现连线。只更新小禾的 Aru 条目，新标签与来源标签合并，保留 Aru 标题和原文措辞，OB 副本为正文中实际出现的专名加上双链。已有打标结果的旧条目只补专名。结构化记忆末尾的「来源对话（北京时间）」来自该条记忆关联的原始消息发送时间范围。所有关联消息均可核实时，OB 的创建时间取第一条来源消息的发送时间，并以 imported_at 保留实际入库时间。缺少可核实的来源时，创建时间采用实际入库时间。Aru 记忆的显示标题采用「创建时间＋原题」，时间精确到秒，原题自带的日期保留。每批最多五条，以来源内容哈希标记进度，重复触发会跳过已完成项。结果和收件进度分开显示，关闭手机页面也会继续处理。

## VPS 上的服务

| systemd 单元 | 监听 | 数据 |
|---|---|---|
| `nian-mind` | `127.0.0.1:18110` | `/var/lib/nian-mind` |
| `nian-ob` | `127.0.0.1:18001` | `/var/lib/nian-ob` |
| `nian-gateway` | `127.0.0.1:18111` | `/var/lib/nian-gw/turns.sqlite` |

Caddy 把公网 HTTPS 的 `/nian/*` 转发给网关。OB 的预分析记忆接收接口只供网关在本机调用，校验独立内部令牌。服务使用各自的 Linux 账号。环境配置位于 root 专用的 `/etc/xinchao-nian/`。生产依赖使用 OB 发行版的 Python 锁文件和主机已有的 Node，未安装 Docker。

睡眠梦境先使用心潮原有的消化与召回。需要 Aru 素材时，以最新可用结构化记忆的原始对话结束时间为终点，取此前 48 小时内结束的全部可用条目，含两个边界。上传时间和当前时间不参与计算。按来源时间从新到旧，每场最多取两条；模型写成的梦落盘时一并保存轮到的位置，到末尾后从头继续。上传、编辑或删除会重新确定候选范围，上次的位置已移出窗口时从最新一条开始。已删除、已归档、禁止浮现、缺失原始对话时间及技术事务类条目会跳过。

上游版本固定为 [心潮念 4.3.0，444e7db2](https://github.com/tianyupaipai-cmd/xinchao-nian/tree/444e7db2d138ad7ff23cafd774a5c0d3eb37aae5)。代码安装目录为 `/opt/xinchao-nian/444e7db2`，网关目录为 `/opt/xinchao-nian/gateway`。集成改动见 `upstream.patch`、`aru-dream.patch`、`aru-dream-window.patch` 和 `restless-wording.patch`。通过 `scripts/prepare-upstream.sh` 可将补丁和六个附加 Python 模块装入上游工作树。补丁也将心潮监听地址设为回环。上游署名和分目录许可见原发行包的 NOTICE 与 docs/LICENSING.md。

## 检查运行情况

```sh
systemctl is-active nian-mind nian-ob nian-gateway
cd /opt/xinchao-nian/gateway
sudo -u nian-gw /opt/xinchao-nian/venv/bin/python -B -m unittest -v test_integration
```

测试使用临时目录、隔离的心潮与 OB 实例和模拟模型，不会把测试偏好写入正式记忆库。查看队列状态时可以只输出各状态的条数。

```sh
sudo -u nian-gw python3 -c "import sqlite3; d=sqlite3.connect('/var/lib/nian-gw/turns.sqlite'); print(d.execute('select state,count(*) from turns group by state').fetchall())"
```


### 心潮消息日志

使用 Aru 备份上传账号打开 `/aru-backup/xinchao-log`。网关在普通聊天请求取得上游 HTTP 状态后记录实际提交的聊天消息（含助手历史）及模型返回的可见回复、工具调用，保留原位置与角色。连接失败单独标记。文字保留原文，媒体附件只记类型。手机收件栏按 Aru 回执记录解封核对过的通知。两栏按各自记录时间滚动保留 7 天，定时器每 15 分钟清理过期记录，新日志使用压缩存储。日志用于回看，网关不会把它拼进模型输入。

模型请求以客户端当次的消息数组为准。客户端复用轮次 ID 时，网关也校验截至最后一条用户消息的请求内容，以区分删改和重试。

可选的 `NIAN_UNWRAP_WAKE_SHA256` 接受用空格分隔的 SHA-256 摘要，每个摘要对应一条完整的 UTF-8 外部唤醒消息（含 Aru 的包装）。普通聊天转发时，匹配的 user 消息只发送包装内的原文。默认留空。会话匹配和桥消息识别使用客户端原始输入，日志记录转发后的内容；手机本地数据由 Aru 管理。
