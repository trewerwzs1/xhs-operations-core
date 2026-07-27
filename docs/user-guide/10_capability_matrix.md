# V2 交付能力矩阵

本表区分真实证据、程序证据和运行时条件。`conditional` 不是代码缺失，
表示接收方必须提供真实素材、可见 inbox item 或稳定目标，系统才会执行。

| 工作流 | 能力 | 当前证据 | 接收方条件 |
|---|---|---|---|
| Setup | 安装、Plugin、Skill、扩展 staging、Bridge/专用 Chrome | 2/2 clean install + program pass | 首次加载扩展并扫码登录 |
| Setup | 当前账号身份绑定 | live verified | 登录账号可见 |
| Setup | AccountVoice 学习 | program pass / optional | 有本人历史回复；不足时中性语气 |
| Engage | 三种输入转 TaskIntent | program pass | 账号帖子、指定帖子或 direct brief |
| Engage | 一次搜索、同批候选顺序处理 | live verified | 公开搜索结果可见 |
| Engage | 帖子点赞自主主链 | live verified | 新目标、未点赞、风险与节流通过 |
| Engage | 顶层评论、评论点赞、评论回复 | historical live + autonomous program pass | exact 目标与事实安全 |
| Engage | 主动单条私信 | program pass / conditional | 用户任务明确要求且有稳定 peer ID |
| Service | 评论/DM inbox scan | live verified empty batch | 登录账号 inbox 可见 |
| Service | 单项被动回复 | program pass / conditional | 出现合格可见 item |
| Publish | 图片/视频发布纵向链 | program pass / conditional | 完整本地媒体、正文与发布事实 |
| Review | OperationLedger status/list/export | program pass + live verified Engage record | 无额外条件；只读 |

所有真实平台动作只允许
`XhsOperationGateway -> ranfang_run_agent -> XHS Bridge -> dedicated Chrome`。
每个 heartbeat 最多一个动作，账号写入至少间隔 600 秒。unknown 不重试，
无法只读对账时停止。

用户下达任务即构成该任务的有界执行委托。StrategyPack、Campaign、计划、
PolicyDecision 和一次性 ActionPermit 由 Codex 与包内程序在内部生成；普通
产品流程不要求用户复制确认口令、hash 或逐动作授权。
