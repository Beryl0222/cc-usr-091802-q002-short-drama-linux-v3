# 海外短剧制片协作

服务网文改编短剧的跨市场制作后端，覆盖横琴基地三十多个国际制片团队并发协作的核心痛点：**谁在什么权利范围内、基于哪个批准版本、用了哪些素材拍了什么、发行到哪些市场**，全程可追溯、可对账。

- 纯 Python 标准库 + SQLite（WAL），无新增依赖（`requirements.txt` 仅测试用 pytest）
- 所有写操作在 `BEGIN IMMEDIATE` 事务内完成“检查 + 写入”，配合唯一约束兜底并发
- 时间统一 UTC 存储，按 IANA 时区展示团队/资源本地时间

## 运行

```bash
python3 service.py --check                 # 基础自检
python3 service.py --port 8000             # 启动 API（默认 data/production.db）
python3 -m unittest tests.test_production  # 35 项契约/并发/HTTP 测试
```

健康检查：`GET /health`。

## 领域规则与需求对应

| 运营诉求 | 实现 |
| --- | --- |
| 项目先锁定来源 IP、可改编市场、语言、期限和素材边界 | `ips` / `projects` / `project_markets` / `allowed_source_materials`；授权（`licenses`）按市场+语言+期限授予，可对某市场从边界中剔除素材（`restricted_materials`） |
| 本地化意见与剧情改动逐条关联 | `script_notes` ↔ `script_changes`（`source_note_id`），每条意见只能被处理一次；按场景挂载 |
| 版权、合规、制片审批汇合才能下发拍摄 | 三方 `approvals` 门禁：版权批校验素材越界、合规批要求合规意见清零、制片批要求文化意见均被改动处理；汇合瞬间锁定版本并快照当时授权条款 |
| 并发改稿不串版 | 剧本版本内容哈希 + 递增 seq + 乐观并发（`based_on_version_id` 必须是 head，否则 409 `version_conflict`） |
| 档期冲突给出明确原因 | `slot_minutes` 分钟级占用表（资源+分钟主键）；冲突返回场地/演员/团队三类原因，含占用方项目/场景/档期、重叠分钟与本地时区时间 |
| 已拍镜头不因剧本更新失去来源 | 镜头在拍摄瞬间固化 `version_id/seq/content_hash`（`immutable=1`）；有镜头的档期不可取消 |
| 粗剪/字幕/配音/市场版式派生谱系 | `assets` 父链：`rough_cut → subtitle|dub → market_format`；粗剪只能用同一锁定版本拍的镜头；任意资产可 `/lineage` 回溯根镜头 |
| 撤权阻断未发行版本、不误删他市场资产 | 撤权只把同 project+market+language 下 `scheduled` 发行单置 `blocked`；`released` 保留、其他市场行不命中，资产从不删除 |
| 发行回执重复或迟到只入账一次 | `idempotency_key` 与 `(release_id, 结算周期)` 双唯一约束，`ledger_entries.receipt_id` 再唯一；重复请求返回 `duplicate=true` 且不写账 |
| 权利争议可反查 | `GET /api/releases/{id}/trace`：上线时授权条款快照、当前授权状态、采用剧情版本、三方批准人、素材边界、根镜头、团队时区与带本地时间的完整制作事件线 |
| 跨区撤权后各市场账目一致 | 分市场/币种账本与回执双向对账（`/accounts` 的 `balanced`）；金额 Decimal 定点存储 |

## API 摘要

```
POST   /api/ips
POST   /api/projects
POST   /api/teams|/api/sets|/api/talent
POST   /api/projects/{p}/teams
POST   /api/projects/{p}/licenses            GET  .../licenses
POST   /api/licenses/{id}/renew|/revoke
POST   /api/projects/{p}/scripts             GET  /api/scripts/{id}
POST   /api/scripts/{id}/notes|changes|approvals
GET    /api/scripts/{id}/gate
POST   /api/projects/{p}/bookings            GET  /api/bookings/{id}
POST   /api/bookings/{id}/cancel|/shots
POST   /api/projects/{p}/assets              GET  /api/assets/{id}/lineage
POST   /api/projects/{p}/releases
POST   /api/releases/{id}/publish
POST   /api/releases/{id}/receipts           GET  .../receipts
GET    /api/releases/{id}/trace
GET    /api/projects/{p}/accounts|/events
```

错误统一为 `{error, message, details}`，状态码：404 不存在、409 状态/版本/排期冲突（排期冲突的 `details.reasons` 为逐条原因）、422 参数校验失败。

`fixtures/sample.json` 描述横琴基地与协作团队的示例规模（30 个国际团队、90 个棚、年产 3000 部）。仓库不包含真实剧本、演员证件或未公开素材。
