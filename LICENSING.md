# 许可与来源

| 文件 | 来源与适用条款 |
| --- | --- |
| 独立编写的网关、导入工具、通知适配器、测试和部署文件 | AGPL-3.0-only，全文见根目录的 [LICENSE](LICENSE) |
| `upstream.patch`、`aru-dream.patch` 和 `aru-dream-window.patch` 中的 `xinchao/` 修改 | 心潮引擎，沿用 [MIT](licenses/upstream/xinchao/LICENSE) |
| `upstream.patch` 中的 `ombre-brain/` 修改 | OB 原始代码沿用 MIT，CyberSealNull 衍生部分仅限个人、学习和非商业使用，商业使用需书面许可。见 [原始 MIT](licenses/upstream/ombre-brain/LICENSE.P0luz-MIT)、[衍生条款](licenses/upstream/ombre-brain/NOTICE.CyberSealNull.md) 和 [上游修改记录](licenses/upstream/ombre-brain/MODIFICATIONS.md) |
| `xinchao-now-hook.sh` 和 `aru_wake/runtime/` | xinchao-runtime-bridge，MIT，包含原许可 |
| `aru_wake/aru/wake-send.mjs` | Aru Host v0.39.2，Apache-2.0，包含原许可 |

心潮引擎的补丁改动涉及互动分类的提示词与结果归一化、服务端冷却检查和事件处理、MCP 工具目录与路由，以及本机监听地址。OB 的补丁改动涉及内部导入接口、记忆存储和元数据、列表与搜索结果、面板标题和正文双链展示。独立的 Aru 导入模块负责读取、对比和补全备份中指定协作者的四类记忆。

心潮念联合发行版的署名与许可说明随附于 [NOTICE](NOTICE) 和 [上游许可索引](licenses/upstream/docs/LICENSING.md)。通知桥与 Aru 加密代码分别取自 [xinchao-runtime-bridge](https://github.com/tianyupaipai-cmd/xinchao-runtime-bridge/tree/82f78902aae6df0aec314c2c6d5ced6632c5f18a) 和 [Aru Host](https://github.com/Aevella/aru-host/tree/v0.39.2)。
