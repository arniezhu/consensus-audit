# Consensus Audit

[English](README.md) | 简体中文

一个 agent skill：把不平凡的工作卡在**独立的对抗性评审**之后，而不是让 agent 自己给自己背书。

在 agent 敲定设计或交付实现之前，`consensus-audit` 会派出两到三位各自独立看这份工作的
reviewer，解析他们的结构化裁决，收敛分歧，再决定这件事能不能往下走。它只对**有据可查、可复现、
真正阻断执行**的 blocker 放闸；并且 fail-closed——reviewer 输出解析不了、调用失败，一律记为
blocker，绝不当作通过。

支持 **Codex**（`codex exec`）和 **Claude Code**（`claude --print`）。

> **第一次运行前请看。** Reviewer 使用的模型默认指向本 skill 开发时所用的档位
> （`gpt-6.1-sol`）。模型预检是 fail-closed 的，如果你的账号没有这个模型，
> **不设 reviewer 模型就会每次运行都被拦住**——见
> [配置模型](#配置模型)。Claude Code 后端下默认值会自动切到 Claude 模型，通常不受此影响。
>
> **一轮不便宜。** `--risk-level high` 下，一轮是**三个独立的 reviewer 会话**；未关闭的 blocker
> 达到两条及以上时，每位 reviewer 还要各做一次交叉评审，变成**六个**。另外每个不同的 reviewer 模型
> 要做一次预检，输出解析不了的评审还会多一次重格式化调用。标准档是两个，有交叉评审时是四个。
> 对着大仓库跑之前先估算一下开销。

## 为什么需要它

当前的前沿模型经常能一次写出可用且结构良好的代码。能力提升改变不了的是：模型评审自己的产出时，
继承的是产生这份产出的同一套推理，因此**最容易存活下来的错误，恰恰是当初看起来最合理的那些**。
追问一句"你确定吗"，多半只会换来附和——而模型越强，那句附和越有说服力。

这个 skill 用三件事替掉那种自查：reviewer 跑在**彼此独立的会话**里，在交叉评审之前谁也看不见谁
的结论；每位 reviewer 扛的是**不同的职责**，不是同一把尺子量三遍；最后的主持裁决**只能拿能复现
的证据卡人**，拿不出证据的意见不构成阻断。

## 工作方式

两个阶段，产物都落在 `.discuss/<topic-slug>/round-<NN>-<phase>/`：

- **prework** —— 动手实现之前、或设计定稿之前跑。
- **postwork** —— 实现完成之后、最终交付之前跑。

每一轮都会产出主 agent 的 baseline、各 reviewer 的独立结论和本轮的共识结论；未关闭的 blocker
达到两条及以上时会加一轮交叉评审，达到轮次上限或 blocker 停滞时会要求主持裁决。所有
产物都写盘，所以一个"可以放行"的结论事后查得到依据，而不是只能选择相信。

Reviewer 数量随声明的风险等级伸缩：

| Reviewer | 职责 | 何时参与 |
|---|---|---|
| Tim Sir（高彦博） | 协议完整性、依赖一致性、架构契合度、证据门禁逻辑 | 始终 |
| Sam Koo（古泽琛） | 取证式证据、可复现性、计划与实际的对账、边界情况追溯 | 始终 |
| Madam Leung（梁小柔） | 发布安全、回滚方案、护栏、运行影响半径 | `--risk-level high\|critical` |

## 安装

**依赖：** `python3` 和 PyYAML（`python3 -m pip install pyyaml`）。runner 用它读取
`config/*.yaml`；缺少它时，按文档方式发起的每一轮都会以退出码 1 结束。

可安装的包是 `skill/` 目录。把它复制到 agent 的 skills 目录，**并命名为 `consensus-audit`**：

**首次安装：**

```bash
# Claude Code
cp -r skill ~/.claude/skills/consensus-audit

# Codex
cp -r skill ~/.codex/skills/consensus-audit
```

**升级已有安装时，单独用 `cp -r` 是不安全的。** POSIX 的 `cp -R src dir` 在目标已存在时会把源
**拷进**目标，于是产生 `~/.claude/skills/consensus-audit/skill/`，而旧的 `SKILL.md` 和
`scripts/` 仍留在顶层——agent 会继续加载旧版本，而升级看起来是成功的。应当整棵替换：

```bash
rm -rf ~/.claude/skills/consensus-audit
cp -r skill ~/.claude/skills/consensus-audit
```

这种写法在验证新副本之前就删掉了旧副本，新副本一旦有问题，装着的就是坏的。如果这是你依赖的安装，
请改用 [`skill/00-READ-FIRST.md`](skill/00-READ-FIRST.md) 里的分阶段安装：它先对暂存副本跑一轮
smoke round，通过了才替换已安装的副本。不保留旧版本备份，回滚靠 git 历史。

目标名不能省：安装和验证流程里引用的都是 `consensus-audit`。写成
`cp -r skill ~/.claude/skills/` 会装成 `~/.claude/skills/skill/`，上述流程就对不上了。

**装在哪里决定了用哪个后端**：`--agent auto` 在 `~/.codex/skills` 下用 Codex reviewer，在
`~/.claude/skills` 下用 Claude Code reviewer。`--agent codex` / `--agent claude` 只能用来复述
已探测到的结果，不一致会被拒绝，而不是默默照办。

需要稳妥的安装方式（先暂存副本、验证通过再替换），见
[`skill/00-READ-FIRST.md`](skill/00-READ-FIRST.md)。

## 使用

装好即可，不需要额外配置。之后有两种触发方式：

- **agent 自动触发** —— 遇到设计方案、架构决策、重构、代码改动、关键文档变更，或涉及安全、数据、
  生产环境的工作时，agent 会自行判断需要走审计。
- **你显式要求** —— 直接说「审计」「共识审计」「三方审计」「跑一下 prework」「postwork」之类即可。
  英文说 "consensus audit" 同样可以触发。

一轮跑完，结论和全部产物都在 `.discuss/<topic-slug>/` 下，可以直接翻阅。

**`NEEDS_ANOTHER_ROUND` 不等于"不能发"。** 它只表示还有未关闭的 blocker，哪些真的构成阻断，
由你判断。

**不要把轮次当作收敛信号。** 修复会牵出新问题，其中一部分正是修复自己引入的。本仓库发布前自审的
未关闭 blocker 数是 **5 → 4 → 10**——每一轮都关掉了上一轮提出的全部问题，又开出更多，而且
**没有任何一条 blocker 在轮次之间被延续**。轮次在涨，残余风险跟着一起涨。内置的
`stagnation_count` 和 `STALE_STAY` 追踪的是**反复出现**的同一条 blocker，所以在这种形状的运行里
它们一次都不会触发。（`STALE_STAY` 根本不由 runner 自己计数：只有主代理传入
`--cross-review-stay-count` 时才会触发，见 [`skill/SKILL.md`](skill/SKILL.md) 的状态说明。）

**退出条件要在开跑之前定，写进 baseline 的 `## Exit Condition` 一节**，趁你还不知道会查出什么：写明"满足什么才可以发"——
例如"不接受任何触及安装、回滚、数据删除路径的 open blocker"——事后拿这一轮去对照它，而不是
对着发现重新解释它。事先写下的是判断，事后写下的是合理化，而做这个判断的人正是作品被审的那一位。

**退出条件是下限，不是上限。** 它列举的是"一定阻断"的东西，并不定义"允许阻断"的全集。落在条件
之外的 blocker——因为你没预料到那个类别——**不因此被免除**。要越过它发布，需要一次显式的接受，
并写进本轮记录：查到了什么、为什么可以接受、谁做的决定。这样一来，条件里的疏漏就从**无声通过**
变成**有痕接受**。这套做法拦不住一个人写下软弱的条件再把什么都"接受"掉；它的意义在于接受会留下
痕迹，而且**条件本身也在 reviewer 读的材料里，可以被提成 blocker**。

**新 blocker 出现时，看它指向哪里。** 落在上一轮修复动过的代码里，那是你刚制造的回归，再修一轮
大概率再造一个；落在此前任何一轮都没触及的地方，那是往下挖，值得付这个成本。
`AUDIT_SUMMARY.md` 区分 `OPEN_BLOCKERS` 和 `DISCARDED_REVIEWER_BLOCKERS`，**被丢弃的那份
也要读**——一条 blocker 可能因为契约细节被丢掉，内容却是对的。

## 配置模型

Reviewer 使用的模型默认指向本 skill 开发时所用的档位。可以按次覆盖
（`--reviewer-a-model` / `--reviewer-b-model` / `--reviewer-c-model`），也可以设一次环境变量：

```bash
export CONSENSUS_AUDIT_REVIEWER_A_MODEL=...   # Tim Sir
export CONSENSUS_AUDIT_REVIEWER_B_MODEL=...   # Sam Koo
export CONSENSUS_AUDIT_REVIEWER_C_MODEL=...   # Madam Leung
export CONSENSUS_AUDIT_CLAUDE_MODEL=...       # Claude Code 后端
```

优先级是明确的：**命令行 flag > 环境变量 > 后端默认值**，环境变量在两个后端下都生效。

模型预检探测的正是本轮 reviewer 实际要用的模型，并且是 fail-closed 的：如果你的账号没有默认档位
的模型，请设置上面的 reviewer 模型，否则每次运行都会被预检拦住。`CONSENSUS_AUDIT_REQUIRED_MODELS`
和 `--required-models` 已废弃，设置后会被忽略。
[`skill/config/role_config.yaml`](skill/config/role_config.yaml) 里的 `model` 只是记录 Codex
后端的默认档位、供 provider 检查使用，改它**不会**改变实际运行的模型。

三位日常 reviewer 在 Codex 下默认使用 `gpt-6.1-sol`，在 Claude Code 下默认使用
`claude-opus-5-5`，两者均显式指定 `high` 推理力度。可用 `--reviewer-a-effort`、
`--reviewer-b-effort`、`--reviewer-c-effort` 覆盖；显式选择 `adaptive` 时只要求协议层自我声明，
运行时力度继承 CLI 配置。

常规主持裁决使用当前主会话。涉及门禁语义、复杂安全边界或难以恢复的数据损坏，且存在有据可查的
疑难分歧且状态达到 `MODERATOR_DECISION_REQUIRED` 时，才单独调用只读裁决：Codex 使用 `gpt-6-astra/high`，Claude Code 使用
`claude-fable-5-1/high`。它们不参与日常 reviewer 调用或常规模型预检。该调用由 skill 的主代理执行并
记录，runner 不会自动启动；`--main-moderator-model` 只记录实际主会话，不能切换模型。
详见 [疑难裁决政策](skill/SKILL.md#difficult-case-adjudication)。

## 语言

播报和进度信息**跟随对话语言**。你用中文说话，两者都是中文；用英文，两者都是英文。不需要任何配置
——agent 每次调用都会把当前会话语言传给 runner，并且逐轮重新判断。

只有在**没有 agent、你手工直接敲 CLI** 时才会走默认值，此时回退英文；要中文就传
`--progress-language zh`。地区标签如 `en-US`、`zh-CN` 都接受。`zh-TW`、`zh-Hant` 会被**明确拒绝**
而不是默默按简体输出——需要繁体请用 `--progress-messages-file` 提供翻译目录。

Reviewer 的裁决、`prework` / `APPROVE` 这类协议标识符，以及 `.discuss/` 下的审计产物，**一律不
翻译**。这是刻意的：解析要稳定，不同语言下跑出来的轮次也要能横向比对。

## 目录结构

`skill/` 是安装单位；与它平级的一切都只属于仓库，不会被安装。

| 路径 | 作用 |
|---|---|
| `skill/SKILL.md` | 激活规则与精简工作流——安装后以它为准 |
| `skill/references/consensus_protocol.md` | 完整的 reviewer 契约、产物结构、失败码 |
| `skill/scripts/consensus_audit.py` | 可选的自动化 runner（重试、失败即拦截的解析、产物生成） |
| `skill/config/` | reviewer 职责边界、证据策略、角色默认值、provider 与回流规则 |
| `skill/00-READ-FIRST.md` | 面向人的安装 / 回滚手册 |
| `tests/` | 测试套件——仅限仓库 |

## 升级说明

提交 `b3d4400` 删除了可选的 canary/full 优化发布、审查缓存、`--break-glass-mode` 和 shadow parity
对比工具。现在每一轮都会实际调用所有参与的审查者。如果你在脚本里调用 runner，请按下表调整：

| 已删除的参数 | 怎么处理 |
|---|---|
| `--rollout-mode` 取值 `canary` 或 `full` | 去掉这个参数。`--rollout-mode shadow` 仍可运行，但会打印弃用警告。 |
| `--cache-mode`、`--canary-percent`、`--canary-seed`、`--auto-degrade` / `--no-auto-degrade`、`--metrics-window`、`--threshold-parse-fail-rate`、`--threshold-timeout-rate`、`--threshold-fallback-revise-rate`、`--force-shadow`、`--state-lock-timeout-sec`、`--state-lock-stale-sec`、`--force-lock-steal`、`--parity-verdict-threshold`、`--parity-blocker-symdiff-threshold`、`--parity-reason-code-threshold`、`--shadow-baseline-round-dir`、`--positive-smoke-round-dir`、`--failclosed-smoke-round-dir` | 直接删掉。它们调节或关闭的功能已经不存在。 |
| `--rollout-readiness-file`、`--shadow-parity-mode`、`--break-glass-mode` | 删掉，并且不要再依赖它们要求的那道检查：那道检查已经不存在了。 |

传入已删除的参数时，runner 会在写任何轮次目录之前报错，说明是哪个参数、该怎么改。
`--adaptive-effort-mode` 从来没有实际作用，现在仍可传入，但会打印弃用警告。
已有的 `.discuss/` 历史无论升级还是回退都不需要迁移。回退方法是 `git revert` 后用
[`skill/00-READ-FIRST.md`](skill/00-READ-FIRST.md) 里的安装命令重装，这条路径实际演练过：
旧版 runner 能把新版写出的主题当作普通历史读取。

## 测试

在仓库根目录执行：

```bash
python3 -m unittest discover -s tests -p "test_*.py"
```

## 声明

Reviewer 的角色名取自 TVB 剧集《法證先鋒》，属致敬性质。本项目与 TVB 无隶属关系，亦未获其背书。

## 许可

MIT，见 [LICENSE](LICENSE)。
