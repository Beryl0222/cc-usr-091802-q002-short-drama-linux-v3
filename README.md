# 海外短剧制片协作（横琴海外短剧基地）

服务网文改编短剧的跨市场制作：三十多个国际制片团队共用一套后端，确保版权方、
编剧、本地顾问、制片人与发行平台始终基于**一致的授权范围与剧本版本**开工。

`python3 service.py --check` 检查基础环境，`python3 service.py --port 8000`
启动服务，`GET /health` 查看运行状态。

## 领域规则（代码即规则，测试即契约）

1. **立项先锁权利**：每个项目按市场锁定「可改编市场、语言、期限、素材边界」，
   锁定必须被仍有效的授权完整覆盖（语言子集、期限在窗口内、素材不越界）。
2. **本地化意见逐条关联剧情改动**：本地顾问的文化意见挂到具体剧本条目，
   编剧的剧情改动 1:1 关联该意见，二者随剧本版本快照固化。
3. **三方审批汇合才可下发**：版权（rights）、合规（compliance）、制片
   （production）全部批准后版本才 `approved`，按市场下发拍摄；任一方可驳回，
   审批中的版本不能并行改稿。
4. **并发改稿乐观控制**：新版本的父版本必须是当前 head，后提交者拿到带最新
   head 的冲突，必须基于新版本重新修订。
5. **档期冲突原因明确**：场景/演员/团队各带 IANA 时区，冲突返回冲突方、
   项目、UTC 与资源本地时间；并发抢档恰好一方成功。
6. **已拍镜头来源固化**：拍摄时把授权条款、权利窗口、剧本条目、三方批准人、
   素材边界与档期整体快照进镜头；剧本再更新不会改动或丢失镜头来源。
7. **派生谱系**：粗剪 → 字幕/配音 → 市场版式，每条资产可回溯到镜头与已下发
   剧本版本；语言不得超出市场锁定语言，镜头不得超出父资产来源。
8. **按市场撤权**：尚未发行的版本一律冻结（`blocked`，不能再派生/发布）；
   已发行版本标记召回（`recalled`）但**资产与历史回执保留**；其他市场资产
   一律不动。撤权与级联在同一事务内原子完成。
9. **回执幂等**：幂等键 + 平台回执单号双道去重，网络重试或迟到的重复回执
   只入账一次；分市场独立汇总，撤权市场账目冻结且不串市。
10. **争议反查**：任一上线版本出现权利争议，可沿授权条款、团队时区与制作
    事件，反查实际采用的剧情、三方批准人、素材与权利窗口（拍摄时条款与
    当前条款并列展示）。

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `studio/licensing.py` | 来源 IP、市场授权窗口、项目立项锁定、撤权 |
| `studio/script.py` | 版本链、本地化意见/剧情改动、三方审批与下发 |
| `studio/schedule.py` | 时区感知资源、档期冲突判定与明确原因 |
| `studio/shoot.py` | 镜头拍摄与来源快照固化 |
| `studio/asset.py` | 派生谱系、发布、撤权级联冻结/召回 |
| `studio/ledger.py` | 回执幂等入账、分市场账目 |
| `studio/trace.py` | 权利争议反查 |
| `studio/events.py` | 只追加的制作事件日志 |
| `studio/app.py` | 领域外观（跨模块事务，如撤权+级联） |
| `studio/api.py` | HTTP JSON 适配层 |
| `studio/store.py` | 单锁串行化的线程安全内存仓储 |

## 主要 HTTP 接口

所有写操作为 `POST /api/...`，领域错误返回结构化 JSON（400 校验 / 404 不存在
/ 409 状态或并发冲突）。

- `POST /api/ips`、`POST /api/grants`、`POST /api/projects`
- `POST /api/projects/{id}/localization-notes`、`.../plot-changes`
- `POST /api/script-versions`、`POST /api/script-versions/{id}/{submit|approve|reject|withdraw|release}`
- `POST /api/resources`、`POST /api/bookings`、`POST /api/shots`
- `POST /api/assets`、`POST /api/publish`、`POST /api/revoke`、`POST /api/receipts`
- `GET /api/resources/{id}/calendar`、`GET /api/assets/{id}/lineage`
- `GET /api/trace/release/{id}`、`/api/trace/asset/{id}`、`/api/trace/shot/{id}`、`/api/trace/project/{id}/market/{market}`
- `GET /api/projects/{id}/accounts`、`GET /api/events?project_id=...`

仓储为进程内状态，重启清空（与基线脚手架一致）；领域服务可直接在进程内
使用 `studio.ProductionSystem`。

## 测试

```bash
python3 -m unittest discover -s tests -v   # 50 个用例
python3 -m unittest                         # 基线契约（服务标识、样例数据）
```

覆盖：立项越权（语言/期限/素材）、三方审批汇合、并发改稿、8 路并发抢档、
撤权×12 路发布竞争、撤权期间跨市场账目压力（60 笔无串市、重复回执至多 1 笔）、
撤权不误删他市/已发行资产、拍摄快照抗剧本更新、完整 HTTP 端到端流程。
