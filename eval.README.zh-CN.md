# 一条命令启动网关和评测

从仓库根目录运行。首次使用请按[运行环境说明](claude_code/README.zh-CN.md)
准备 Python 环境、Phoenix 私有配置和所选任务的 Docker 镜像。
`config.claude_code.json` 登记了 104 个任务的环境配置，镜像需另行准备。

## 配置在哪里修改

日常更换网关、后端地址或路由时，编辑 `.nl2repo-local/gateway.env`。
可参考根目录的 [gateway.env.example](gateway.env.example)，将需要的条目合并进去；
已有私有文件不要直接覆盖。`eval.sh` 每次启动会自动读取该文件。

| 要修改的内容 | 配置入口 | 单次命令覆盖 |
|---|---|---|
| 路由类型 | `PHOENIX_ROUTING` | `--phoenix-routing legacy/aws/azure` |
| AWS 的 `x-eval-domain-proxy` | `PHOENIX_MULTICLOUD_PROXY` | `--phoenix-proxy` |
| H100 的 `x-eval-domain-proxy` | `PHOENIX_DOMAIN_PROXY` | `--phoenix-proxy` |
| Azure 的 `x-eval-domain-proxy` | `PHOENIX_AZURE_DOMAIN_PROXY` | `--phoenix-proxy` |
| AWS SGLang 后端地址 | `SGLANG_IP_PORT` | `--phoenix-backend-host` |
| AWS 区域 | `PHOENIX_BACKEND_REGION` | `--phoenix-backend-region` |
| AWS 轨迹 ID | `SANDBOX_TRAJECTORY_ID` | `--phoenix-trajectory-id` |
| 默认模型、采样参数、租户信息 | `.nl2repo-local/litellm.phoenix*.yaml` 私有模板 | `--model` 等参数；`--phoenix-config` 选择模板 |
| 任务镜像、评测策略、模型超时 | `config.claude_code.json` | `--config`、`--model-timeout-seconds` 等 |

代理地址的优先级为：**命令行 → 对应路由的环境变量 → 适用的模板值 → 代码默认值**。
AWS 只有在模板包含 `X-Backend-Host` 时才继承模板中的代理及轨迹 ID，
否则使用多云默认网关，并要求另行提供轨迹 ID。Azure 使用独立的环境变量和模板。
令牌规则见下方各路由说明。

例如，将下面一行加入 `gateway.env` 即可修改 AWS 的默认网关：

```bash
export PHOENIX_MULTICLOUD_PROXY="${PHOENIX_MULTICLOUD_PROXY-your-gateway.example.com}"
```

这种写法保留调用命令前显式设置的环境变量；若写成无条件 `export VAR=值`，
则文件会覆盖同名 shell 变量。空值不会自动回退，必填项会在启动前报错。
配置修改只影响新启动的实验。`experiments/<name>/gateway.yaml` 是生成快照，
应从上述入口修改配置后启动新实验。

代码维护入口为 [claude_code/phoenix_config.py](claude_code/phoenix_config.py)：
`ROUTES` 集中定义三种路由的 API 地址、默认模板与代理环境变量；各路由分别解析请求头，
统一处理超时、验证和凭据引用。`launch.py` 使用解析结果组装实验。

## Phoenix 单任务验证

```bash
PHOENIX_DOMAIN_PROXY='proxy_33.59.171.170:23456' ./eval.sh \
  --name phoenix-smoke-005 --tasks six --concurrency 1
```

启动器自动创建独立实验目录、分配本机空闲端口、启动 LiteLLM、等待网关鉴权就绪，
再后台启动 Claude Code 评测。评测完成或失败后，关闭本次创建的网关。
无需另开终端启动网关，也无需手动设置本机网关密钥。

`--name` 必须是新名称；已有实验不会覆盖或追加。使用 `--foreground` 前台运行，
Ctrl-C 会停止本次评测及网关。命令可以使用绝对路径从其他目录执行。

## 更换代理目标或模型

以下两种写法等价，命令行参数优先于环境变量：

```bash
PHOENIX_DOMAIN_PROXY='proxy_33.59.171.170:23456' ./eval.sh \
  --name phoenix-smoke-006 --tasks six --concurrency 1

./eval.sh --phoenix-proxy 'http://33.59.171.170:23456' \
  --name phoenix-smoke-007 --tasks six --concurrency 1
```

默认 `--phoenix-routing legacy` 沿用 H100 访问方式。
也接受 `33.59.171.170:23456`，不带协议时自动补 `http://`。
完整 HTTPS 地址会保留协议；代理目标不要添加 `/eval/v1`。
每次生成的网关配置都会同步设置：

```yaml
api_base: http://phoenix-gw-eval.alibaba.com/eval/v1
extra_headers:
  x-eval-domain-proxy: http://33.59.171.170:23456
  X-Backend-TrajectoryID: proxy_33.59.171.170:23456
```

请求经 Phoenix 的 `/eval/v1/chat/completions` 转发给 SGLang。
`x-eval-token`、额外请求头、超时和实际模型名默认继承
`.nl2repo-local/litellm.phoenix.yaml`，模板只读，不会被不同实验相互覆盖。
不传代理目标时，读取 `PHOENIX_DOMAIN_PROXY`，再回退到模板中的目标。
可以通过 `PHOENIX_EVAL_TOKEN` 覆盖 Phoenix 令牌，通过 `--model <served-model-name>`
覆盖上游模型名；模型名不带 LiteLLM 的 `openai/` 前缀。

脚本自动读取 `.nl2repo-local/gateway.env`。要保存新终端的默认代理目标，可在其中加入：

```bash
export PHOENIX_DOMAIN_PROXY="${PHOENIX_DOMAIN_PROXY:-proxy_33.59.171.170:23456}"
```

Phoenix 模式自动生成本次网关密钥。历史 `NL2REPO_API_KEY` 只在复用已有网关时需要。
实验中的 `gateway.yaml`、`launch.json` 仅保存密钥的环境变量引用。
实际运行配置写入权限 600 的临时文件，并在网关退出后删除；私有令牌不复制进实验配置。

## B200 多云网关

B200 使用 `--phoenix-routing aws`。Phoenix API 地址不变，但
`x-eval-domain-proxy` 改为多云网关域名，SGLang 地址单独放在 `X-Backend-Host`。
`X-SMG-Routing-Key` 与 `X-Backend-TrajectoryID` 必须使用同一个平台提供的轨迹 ID，
不再从后端 IP 派生。AWS 必须设置 `PHOENIX_EVAL_TOKEN`，不会继承旧 H100 模板中的 token。
令牌可写入 Git 忽略的 `.nl2repo-local/gateway.env`，不要写入代码或公开配置。
以下环境变量使用实际平台配置值：

```bash
export SGLANG_IP_PORT='<B200 SGLang IP:端口>'
export SANDBOX_TRAJECTORY_ID='<平台提供的轨迹 ID>'
export PHOENIX_EVAL_TOKEN='<AWS eval token>'

./eval.sh --name b200-smoke-001 \
  --phoenix-routing aws --model default \
  --tasks six --concurrency 1 --model-timeout-seconds 3600 \
  --max-token 16384
```

命令末尾添加 `--dry-run` 可只检查生成配置，不启动评测。凭据和多云路由 ID
在输出中以环境变量引用表示；实际请求时解析为真实值。
此示例启动一次完整的 six 评测；原始 curl 的 `max_tokens: 32` 仅适合 pong 探测，
不应作为代码生成预算。`--max-token` 设置每次上游请求的 `max_tokens`，
包含思考与正文的总输出上限；`--max-tokens` 和原有 `--max-output-tokens` 是等价写法。
该参数沿用宿主机转发层的强制覆盖，避免被 CLI 的默认输出限制截小。

这条命令会生成以下请求头（`${...}` 表示从上述环境变量取值）：

```text
x-eval-token: ${PHOENIX_EVAL_TOKEN}
x-eval-timeout: 3600
x-eval-domain-proxy: accio-agentic-rl-multicloud-gateway-aws.vipserver:80
X-Backend-Host: ${SGLANG_IP_PORT}
X-Backend-Region: us_aws
X-Backend-Timeout: 3600
X-SMG-Routing-Key: ${SANDBOX_TRAJECTORY_ID}
X-Backend-TrajectoryID: ${SANDBOX_TRAJECTORY_ID}
```

也可以显式传入 `--phoenix-backend-host`、`--phoenix-backend-region`、
`--phoenix-trajectory-id`；命令行优先于环境变量，再回退到多云模板中的对应请求头。
后端地址必须为 `host:port`，不能带 `http://` 或 `/v1`。
轨迹 ID 缺失时启动器直接报错，不会生成一个替代 ID。

多云网关域名可以通过 `--phoenix-proxy` 或 `PHOENIX_MULTICLOUD_PROXY` 覆盖；
输出的域名不带 `http://`。为避免沿用 H100 地址，aws 模式不读取
`PHOENIX_DOMAIN_PROXY`；也不会继承缺少 `X-Backend-Host` 的旧模板中的代理和轨迹 ID。
区域读取 `PHOENIX_BACKEND_REGION`，再回退到模板或 `us_aws`。
`PHOENIX_ROUTING=aws` 可以设置默认路由，显式 `--phoenix-routing legacy`
可切回 H100。

多云模式将本地模型通道、LiteLLM timeout、`x-eval-timeout` 和 `X-Backend-Timeout`
同步为同一秒数。优先使用 `--model-timeout-seconds`，然后是基础评测配置中的
`model_timeout_seconds`，再取 Phoenix 模板的 `timeout`，最后默认 3600。
修改配置不会改变已启动的实验。实际 B200 连通性仍需用有效的后端地址和轨迹 ID 验证。

## 云 API（Phoenix Azure，无需 GPU）

使用 `--phoenix-routing azure`，启动器默认读取独立私有模板
`.nl2repo-local/litellm.phoenix-azure.yaml`，通过本地 LiteLLM 将 Claude Code 的
Anthropic Messages 请求转换为 Chat Completions，再发送到
`http://phoenix-gw-eval.alibaba.com/eval/azure/chat/completions`。
此地址不添加 `/v1`。这条路径使用 OpenAI 兼容协议，不使用 LiteLLM 的 Azure
deployment/API-version 路由。

按以下结构创建私有模板（JSON 格式），
填入实际令牌与账号信息，并执行 `chmod 600 .nl2repo-local/litellm.phoenix-azure.yaml`：

```json
{
  "model_list": [{
    "model_name": "repo-model",
    "litellm_params": {
      "model": "openai/gpt-5.6-luna",
      "timeout": 120,
      "reasoning_effort": "none",
      "extra_headers": {
        "x-eval-token": "<eval token>",
        "x-eval-domain-proxy": "https://iai.alibaba-inc.com",
        "tenant": "<tenant>",
        "empId": "<employee id>",
        "iai-tag": "test proxy"
      }
    }
  }]
}
```

先启动一个任务检查生成、工具执行与评分流程：

```bash
./eval.sh --name azure-smoke-001 \
  --phoenix-routing azure --model gpt-5.6-luna \
  --tasks retrying --concurrency 1 \
  --max-turns 60 --max-token 8192 --model-timeout-seconds 120 \
  --reasoning-effort none --incremental
```

追加 `--dry-run` 只检查配置，追加 `--foreground` 在前台运行。
检查五项时将任务改为 `six,retrying,python-slugify,jsonlines,pyperclip`，并使用新的
`--name`；并发可从 1 开始。仍需本地 Docker 与对应生成/评分镜像，但不需要 GPU。

评测使用 `stream=true` 和工具调用，因此 curl 的非流式 Hello 成功仅代表基本连通。
已实测此云接口拒绝 `gpt-5.6-luna` 的工具调用与 `reasoning_effort=low` 组合，
要求改用 Responses API 或设置 `none`。因此云模板默认 `reasoning_effort=none`，
命令也显式指定；这与 B200 思考模式不同，比较模型结果时应记录该差异。
启动配置禁止 LiteLLM 自动将此路由改为 `/responses`。
LiteLLM 会发送 `Authorization`；此云网关优先将 Bearer 值识别为 tenant，
因此启动器自动使用与 `tenant` 请求头相同的值，避免 `Bearer dummy` 覆盖正确租户。
`--max-token` 保留单次输出预算；本机 LiteLLM 会为此 GPT 模型映射成
`max_completion_tokens`。不要传 SGLang 的 `enable_thinking` 模板参数。

云路由不继承 `PHOENIX_DOMAIN_PROXY`、`PHOENIX_EVAL_TOKEN` 或 B200 的后端与轨迹头；
令牌默认来自云模板，可通过 `PHOENIX_AZURE_EVAL_TOKEN` 覆盖。
代理目标可通过 `--phoenix-proxy` 或 `PHOENIX_AZURE_DOMAIN_PROXY` 覆盖。
也可使用 `--phoenix-config` 指定另一份云模板。模板支持 `os.environ/VARIABLE`
引用，实验快照中的令牌、tenant、员工 ID 等也只保存环境变量引用。

2026-09-09 验证：非流式请求返回 200；真实转换层及独立 LiteLLM 网关均完成
“流式工具调用 → 工具结果回传 → 正常结束”两轮探测。完整回归 136 项，
129 项通过、7 项可选 Docker 集成测试跳过。此次未启动正式 benchmark 或评分。

## 全量与运行参数

```bash
./eval.sh --name phoenix-full-003 \
  --phoenix-proxy proxy_33.59.171.170:23456 \
  --tasks '*' --concurrency 16 --max-turns 100 --timeout-seconds 7200
```

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--name` | 必填 | 新实验名称 |
| `--mode` | 自动选择 | 未传 `--base-url` 时为 `phoenix`，否则为 `gateway`；可显式指定 `direct` |
| `--phoenix-routing` | `PHOENIX_ROUTING` 或 `legacy` | H100 使用 legacy；B200 使用 aws；云 API 使用 azure |
| `--phoenix-proxy` | 环境变量，再回退到模板或多云默认网关 | legacy 的 SGLang 地址；aws 的网关域名 |
| `--phoenix-backend-host` | `SGLANG_IP_PORT`，再回退到模板 | aws 必填的后端 host:port |
| `--phoenix-backend-region` | `PHOENIX_BACKEND_REGION`、模板或 `us_aws` | 多云后端区域 |
| `--phoenix-trajectory-id` | `SANDBOX_TRAJECTORY_ID`，再回退到多云模板 | 两个路由 ID 请求头使用同一个值 |
| `--phoenix-config` | `.nl2repo-local/litellm.phoenix.yaml`；azure 使用 `litellm.phoenix-azure.yaml` | Phoenix 私有模板，JSON 格式的 YAML |
| `--model` | Phoenix 模板中的模型 | 其他模式必填 |
| `--base-url` / `--sglang-url` | 无 | `gateway` / `direct` 的目标地址；Phoenix 使用 `--phoenix-proxy` |
| `--api-key-env` | `SGLANG_API_KEY` | `gateway` / `direct` 的密钥变量名；默认变量缺失时用 dummy |
| `--config` | `config.claude_code.json` | 基础配置与任务环境登记 |
| `--tasks` | `*` | 全量，或逗号分隔任务名，如 `six,retrying` |
| `--skip-python-version-check` | 关闭 | 跳过生成与评分环境的 Python 主次版本一致性及任务指定版本检查，仍记录实际版本 |
| `--concurrency` | `16` | 本实验任务并发数 |
| `--max-turns` | `100` | 每任务最大 agent 轮数 |
| `--max-token` / `--max-tokens` / `--max-output-tokens` | 继承基础配置或 CLI 默认值 | 上游 max_tokens，单次思考＋正文总 token 上限；宿主机转发层设置 |
| `--context-length` | 继承基础配置或 CLI 默认值 | Claude Code 上下文窗口；上游服务须已支持此长度 |
| `--model-timeout-seconds` | 通道 3600；网关继承模板 | 模型请求超时；同步 `x-eval-timeout`，多云模式还同步 `X-Backend-Timeout` |
| `--chat-template-kwargs` | 继承 Phoenix 模板 | 模型模板参数 JSON；命令行按键覆盖，仅用于 Phoenix/gateway |
| `--reasoning-effort` | 继承 Phoenix 模板 | 上游推理强度；仅用于 Phoenix/gateway，需模型支持 |
| `--incremental` | 关闭 | 增加尽早写入最小可运行实现、用本地测试完善的指令 |
| `--timeout-seconds` | `7200` | 单次生成超时，也是默认的多次尝试共享预算，不含评分 |
| `--generation-budget-seconds` | 同 `--timeout-seconds` | 准备、生成与重试等待的共享时间预算；CLI 优先于配置 |
| `--install-timeout-seconds` | `600` | 原始产物安装阶段总超时；也是每条官方安装命令的上限 |
| `--grading-timeout-seconds` | `1800` | 官方评分镜像构建、安装和测试的合计时间预算 |
| `--generation-retries` | `1` | 仅生成开始前的环境准备重试次数；0 禁用，不重新生成代码 |
| `--evaluation-retries` | `2` | 评分临时故障的额外重试次数；复用已生成代码，0 禁用 |
| `--retry-delay-seconds` | `5` | 首次等待秒数，后续加倍，上限 60 秒；范围 0–60 |
| `--gateway-port` | `0` | 自动分配本机端口，也可指定空闲端口 |
| `--startup-timeout` | `120` | 网关就绪等待秒数 |
| `--keepalive-idle` / `--keepalive-interval` / `--keepalive-count` | `30` / `15` / `4` | TCP 探测参数 |
| `--output-dir` | 仓库的 `experiments/` | 实验输出根目录 |
| `--foreground` | 关闭 | 前台等待完成，返回评测退出码 |
| `--dry-run` | 关闭 | 只打印配置；不启动进程、不写文件、不联网 |

`--tasks '*'` 的引号避免 shell 展开星号。默认路径相对于仓库，显式传入的相对路径
相对于当前工作目录。`NL2REPO_PYTHON` 可以指定启动器的 Python，网关仍使用私有环境中的 LiteLLM。

遇到 `Python version mismatch` 时，可以显式添加 `--skip-python-version-check`。
该选项作用于各任务开始时的环境预检；也可在基础配置的 `claude_code` 中设置
`"check_python_version": false`。实际镜像版本仍会探测并记录，其他环境检查照常进行。
跳过检查不会改变容器中的 Python；生成代码使用较新版本特性时，可能在较旧评分环境中失败。

基础配置当前设置 `check_image_residue: false`，启动器继承这一设置。
它保留 Hook、任务网络隔离和受控 PyPI 通道；详见[完整性说明](claude_code/README.integrity.zh-CN.md)。

启动器完成配置和 Docker 检查后即可启动网关与评测。每个任务获得并发名额后，才拉取自己的
评分镜像并检查环境，通过后立即开始生成；排队任务不提前拉取镜像。`--concurrency 16`
表示最多 16 个任务同时准备或执行。不同评分镜像可以并发拉取，共用同一镜像的任务复用
首次成功拉取并固定的 ID。单个任务准备失败会记录为该任务的 `stage: preparing` 错误，
其他任务继续执行。`benchmark.log` 会记录任务准备以及镜像拉取的开始、完成信息。

Python 版本检查不随 `check_image_residue` 关闭。生成镜像须预先在本地准备；评分镜像配置为
GHCR 等显式仓库地址时，每个评测进程首次使用前会执行 `docker pull --platform linux/amd64`，
拉取失败直接报错，不回退到本地同名标签。拉取后固定镜像 ID，记录在
`offline_environment.runtime.grading`（`source: registry_pull`）中。同一进程重复使用同一镜像时复用该 ID。
显式登记的本地镜像 ID 或本地标签仍使用本地镜像。
两者的 Python 主次版本必须相同；可在任务登记中用 `python_version: "3.10"` 进一步约束。
当前五项 smoke 配置中，six 使用 Python 3.12，retrying、python-slugify、jsonlines、pyperclip
使用 `nl2repo-claude-code:2.1.263-py3.10`。检查允许补丁版本不同，实际完整版本和解析后的
镜像 ID 保存在结果的 `offline_environment.runtime` 中。其他任务也须按各自评分环境准备；
遇到不匹配的任务会在其生成前失败，不能沿用一个 Python 版本覆盖全部任务。

安装验证和官方测试分别记录。`completed` 表示生成及评测流程完成，不代表生成代码正确。
官方断言失败、代码导入/收集错误、测试 setup/teardown 错误可获得有效的部分分数；
`score` 仍为实际通过数，分母使用官方测试总数，未通过或未执行的测试不计分。
生成代码导致的安装失败记录在 `candidate_failures`，不作为评测 infra 故障；
即使因此没有可用的测试分数（`score_valid=false`），评测仍可正常完成（`evaluation_valid=true`）。
Docker/依赖通道故障、pytest 内部错误或中断、统计不完整、执行超时仍标为评测失败。
`coverage_limited` 表示有 error、skip/deselect 或执行数量不完整，不将平台相关 skip 自动当作模型失败。
产物安装及评分在副本上执行，原始工作区保留。轨迹只保存一份格式化的 trajectory.json，
CLI 原始流通过管道直接聚合，不另存原始 JSONL。超时后保存已有内容并清理容器。
评分临时镜像以拉取的官方镜像为基础。在复制产物前，以隔离的 Python 启动方式清除
指向不存在目录的 `.egg-link` 及 `easy-install.pth` 中对应路径，并在构建日志记录修改文件。
这一步不修改官方测试、打包配置、有效依赖安装或官方镜像标签，也不用于原始产物安装验证。

每次创建新 ECS 的平台接入方式见[平台镜像准备与运行说明](claude_code/README.platform.zh-CN.md)。

## 排查只思考、不写代码

`MAX_THINKING_TOKENS=0` 控制 Claude Code 客户端，不能证明经过协议转换后的
上游模型已关闭思考。须核对实际部署的模型、SGLang 版本和模板支持的参数。
以下示例**仅适用于支持 `enable_thinking` 的模型模板**，不会自动替你选择此模式：

```bash
./eval.sh --name phoenix-smoke-thinking-off-001 --tasks six --concurrency 1 \
  --chat-template-kwargs '{"enable_thinking":false}' \
  --max-output-tokens 16384 --timeout-seconds 1800
```

单次总输出上限不是独立思考预算；仍在思考的模型可能耗尽全部预算，导致正文或
工具参数截断。上述数值是 smoke 排查起点，不是全量评测默认值。
`--reasoning-effort none`、模板的 `thinking_budget` 是否有效也取决于上游实现，
不能将参数已传出当成模型已遵守。支持的模板键为 `enable_thinking`（布尔）、
`thinking_budget`（非负整数）和 `reasoning_effort`。

Phoenix 模板的 `litellm_params` 现在继承并校验 `temperature`、`top_p`、
`max_tokens`、`max_completion_tokens`、`reasoning_effort`、`seed`、`extra_body`。
`extra_body` 支持 `chat_template_kwargs`、`top_k`、`min_p`、`repetition_penalty`，
拒绝混入路由和鉴权字段。命令行模板参数逐键覆盖模板，其他设置保留。
`--max-output-tokens` 同时设置客户端环境变量和宿主机转发层的 `max_tokens`，
避免 Claude Code 对未知模型的输出硬上限截小实验配置。显式配置时，启动器移除
托管网关模板中的 `max_tokens` / `max_completion_tokens`，避免旧值覆盖本次上限。
上游实际值以请求诊断为准；direct 模式的外部网关也须允许该值。

### 1M 上下文和单次 393K 输出

当 SGLang 已正确部署为 1,000,000 tokens 上下文时：

```bash
./eval.sh --name qwen38-1m-393k-smoke-001 \
  --tasks six,retrying,python-slugify,jsonlines,pyperclip --concurrency 5 \
  --context-length 1000000 --max-output-tokens 393216 \
  --model-timeout-seconds 14400 --timeout-seconds 28800
```

这里 393,216 是每次模型响应的思考与正文合计上限，不是独立的两段预算。
`--context-length` 显式传入容器的 `CLAUDE_CODE_MAX_CONTEXT_TOKENS`，不会修改
SGLang 的模型配置或上下文容量。两项同时设置时，输出必须小于上下文。
启动器还设置 `CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000` 和
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=90`，目标是在完整上下文容量的 90% 处压缩；
Claude Code 自身的保护阈值仍可能使压缩提前。压缩窗口不再减去输出预算。
如需以 900K 输入为目标，使用 `--context-length 1000000 --max-output-tokens 65536`；
上面的 393K 输出预算无法与 900K 输入同时容纳在 1M 窗口中。
计数仍依赖客户端和网关的 tokenizer，不能据此宣称 token 估算已精确匹配上游。

也可在基础配置的 `claude_code` 对象中添加 `context_length`、`max_output_tokens`、
`model_timeout_seconds`；相应命令行参数优先。配置记录在新实验的配置快照、状态和任务结果中。
已有进程不会热更新。示例将单次模型超时设为 4 小时、每任务生成超时设为 8 小时，
为长输出及多轮操作留出时间。Phoenix 模式同时发送 `x-eval-timeout: 14400`，
但这只是客户端请求的超时值，服务端是否接受、是否限制总时长或流式空闲时间，
以及外部代理是否另有硬上限，仍需按实际部署确认。
本地模型通道的 socket timeout 限制等待网络操作的时间，不是整个 SSE 流的总时长；
`--timeout-seconds` 限制单次生成时长；多次尝试还受共享的
`--generation-budget-seconds` 限制，后者默认等于前者。

对照实验中可单独添加 `--incremental`，鼓励模型读完需求后尽早使用 Write/Edit，
再用小型本地测试消除不确定性。该选项会改变任务提示，记录在实验配置和轨迹中；
所有任务要求及完整性规则保留。比较实验时应记录该选项并尽量一次只改一个因素。

每个新启动的托管网关自动写入 `gateway.requests.jsonl`：

- `upstream_request`：LiteLLM 发给提供方 SDK 的实际请求参数、模型、消息数量和调用 ID。
  `extra_body` 在 SDK 层展开；参数快照不可用时明确标记 `request_parameters_available=false`。
- `upstream_response`：完成的响应所带的 `finish_reason`、用量、耗时，以及正文/思考字符数和工具调用数量。
  `usage_source=litellm_response` 表示来自 LiteLLM 汇总响应，不能视为原始网络分片。
- `upstream_error`：异常类型、状态码和耗时。

日志只记录许可字段，不记录请求头、提示正文、工具内容和异常原文。
用量缺失记为空对象，不把缺失值当成零；LiteLLM 汇总结果中的零仍保留。
模拟测试中，独立的末尾 usage 分片能正确汇总；usage 与 finish_reason 合在同一分片时，
当前 LiteLLM 可能汇总为零。因此零值不能证明上游没有消耗 token，需进一步核对原始分片。
请求有开始记录却没有结束记录时，应结合网关日志判断是否中断。
轨迹也保留 CLI 消息中存在的模型、ID、结束原因和用量；CLI 缺失的信息不会补造。
`direct` 模式使用外部网关，不能生成上述提供方请求诊断。

## 日志、状态和停止

```bash
cat experiments/phoenix-smoke-005/run-state.json
tail -f experiments/phoenix-smoke-005/benchmark.log
tail -f experiments/phoenix-smoke-005/gateway.log
```

后台任务可通过监督进程正常停止：

```bash
kill -TERM "$(cat experiments/phoenix-smoke-005/supervisor.pid)"
```

停止前核对该实验仍在运行及 PID 对应的进程，历史 PID 可能已被系统复用。
启动器只管理本次子进程，不停止已有的外部网关。多个实验可以同时运行，并发数会叠加。

每个实验的主要文件：

- `run-state.json`：运行状态、PID、开始/结束时间、退出码。
- `gateway.log`：本次 LiteLLM 网关日志，可用于定位 Phoenix 的 403 等上游错误。
- `gateway.requests.jsonl`：请求参数、提供方流终止证据与用量诊断（托管网关）；
  provider_stream_end 与 LiteLLM 聚合回调分开记录，后者可能包含估算值。
- `benchmark.log`、`launcher.log`：评测和后台启动日志。
- `config.json`、`gateway.yaml`、`launch.json`：本次配置快照。
- `workspaces/<task>-cc-<uuid>/workspace/`：生成代码。
- 同级 `trajectory.json`：唯一的轨迹文件，UTF-8、两空格缩进、schema_version=2；
  包含思考、正文、工具调用/结果、错误、用量来源及未完成内容，每两秒原子更新，退出时最后保存。
- 同级 `stderr.log`、`workspace.zip`：CLI 错误及评分前的代码归档。
- 同级 `task_state.json`：排队、运行阶段与终态；未进入评测的失败和中断也保存在这里。
- `result/<task>-cc-<uuid>.json`：仅在评测结束后原子写入最终状态和评分，包括评测故障；
  运行中的任务不写入此目录。`evaluation_finished_at` 记录评测结束时间，
  trajectory_path 指向 trajectory.json，不再提供 trajectory_raw_path。

本机 `/v1/models` 就绪只代表网关已启动和本机鉴权正常，不能证明 Phoenix 上游鉴权、
工具调用和多轮执行成功。先检查 smoke 的最终状态及轨迹，再启动全量。
模型返回 403 时优先看 `gateway.log`；若没有生成代码，后续导入目标模块失败是连带结果。

## 直接访问其他服务

可直达的 OpenAI 兼容 SGLang 服务使用 `gateway`，仍自动启动和关闭转换网关：

```bash
./eval.sh --mode gateway --name sglang-smoke-001 \
  --base-url http://GPU:30000 --model my-served-model --tasks six --concurrency 1
```

若有鉴权，设置 `SGLANG_API_KEY`，或用 `--api-key-env MY_KEY` 指定变量名。
此模式不会添加 Phoenix 路由请求头。

已有 Anthropic 兼容接口或本机网关使用 `direct`：

```bash
./eval.sh --mode direct --name existing-gateway-001 \
  --base-url http://127.0.0.1:4000 --model repo-model \
  --api-key-env NL2REPO_API_KEY --tasks six --concurrency 1
```

`direct` 不启动或关闭网关；地址不带 `/v1`，所连接服务必须提供 Anthropic Messages API。
已有网关到上游的连接参数由那个网关自己控制。

## 从旧命令迁移

原来的 `start_phoenix_gateway.sh` 和 `run_phoenix.sh` 已合并到 `eval.sh`。
`--experiment-name` 改用 `--name`，旧全量配置统一为 `config.claude_code.json`。
Phoenix 路由与旧评测结果保持各自独立；`experiments/`、根目录的 `result/`、
`workspaces/` 和历史日志保留原位。


## 任务阶段重试

默认环境准备最多额外重试 1 次，评分阶段最多额外重试 2 次。
为兼容已有命令，准备阶段仍使用 `--generation-retries` 参数名；
该参数不再允许已开始的生成从头重跑，即使旧配置显式设置为 2 或更大。
CLI 参数优先于 `claude_code.generation_retries`、`evaluation_retries`、
`retry_delay_seconds`、`generation_budget_seconds`。
启动器和随仓库提供的配置默认开启；旧配置直接调用 `main.py` 且没有重试字段时，保持不重试。

- 环境准备阶段：仅在生成尚未开始时，在剩余预算内重试拉取镜像等操作中的临时网络/
  容器连接故障。配置错误和镜像完整性检查失败不会触发重试。
- 生成阶段：上游 API 错误和响应流中断由 Claude Code 在原会话内执行请求级恢复。
  当前 2.1.263 镜像已用本地模拟服务验证：503 会重试当前请求；流错误或不完整流
  可以使用相同消息上下文发起非流式请求恢复。模型通道保持请求的 `stream` 取值。
  客户端最终退出后，不再新建会话重新实现项目；保留工作区、轨迹和失败状态。
  正常退出但生成失败时仍对已有产物评分，并保持生成失败/分数无效标记。
  进程异常和生成超时直接记录错误。上下文/轮数限制也不会触发重新生成。
- 评分阶段：重试执行超时、评分容器退出/进程被终止、依赖传输错误等。
  保持同一份生成代码，在新的安装/评分容器中重新评分；不再请求模型。
- 分数低、普通测试断言/收集失败、候选代码安装错误、确定性的缺依赖或依赖版本不兼容，
  不会仅因为被标成 `evaluation_error` 就重试。重试不从多个分数中挑最大值。
- 准备、生成和重试等待共享一份预算，从工作线程领取任务开始计时，排队时间不计入。
  默认预算等于 `timeout_seconds`，可通过 `generation_budget_seconds` 单独设置。
  每次生成最多使用单次超时与剩余共享预算的较小值；预算不足以等待重试时直接结束。
  已在执行的镜像准备和资源清理仍受各自超时控制，因此总墙钟时间可能略超预算。
- 仅失败的准备尝试会归档后重试，已有生成代码不会因此被移走或重置。
  评分不占用生成预算，保留独立的安装/评分超时。等待可被 SIGINT/SIGTERM 中断。

例如，B200 保留 6 小时预算并允许一次环境准备重试，可在原启动命令中加入：

```bash
--timeout-seconds 21600 --generation-budget-seconds 21600 --generation-retries 1
```

如果生成在 2 小时后发生上游错误，客户端可在原会话中重试当前请求，
剩余约 4 小时预算仍可用于完成任务，不会清空代码重新生成。
客户端恢复失败并退出后，任务记录失败，不自动启动新会话或假定能够断点续跑。
客户端请求级重试及非流式恢复均消耗同一份生成预算。

只重试评分阶段：

```bash
./eval.sh --name retry-eval-001 --tasks six \
  --generation-retries 0 --evaluation-retries 2 --retry-delay-seconds 5
```

失败的准备尝试保存在任务目录的 `generation-attempts/<次数>/`（沿用历史目录名），
其 `task_state.json` 指向归档后的轨迹和工作区路径；已生成的代码与轨迹留在原任务目录。
历史实验中该归档目录仍可能包含旧策略重新生成前的代码。评分尝试的结果保存在
`evaluation-attempts/<次数>/result.json`，重试前的日志和依赖审计也保存在该目录。
最终尝试的日志仍位于原任务目录。任务状态包含 `task_attempts`、`task_attempt_count`、
`evaluation_attempts` 和实际 `retry_policy`。中间尝试不增加 `result/` 下的评分行；
终态完成评分后才发布一个结果，生成耗尽且未评分的任务仍记录于 `task_state.json`。
`generation_timeout_seconds` 记录本次实际分配的生成上限；
`generation_retry_stop_reason` 区分生成超时（`generation_timeout`）、
共享预算不足（`generation_budget_exhausted`）、准备重试次数耗尽（`retry_limit`）
和生成已开始、禁止重新生成（`generation_already_started`）。

本策略适用于新启动的评测进程；已运行进程及其排队任务不热更新，也不自动重跑历史实验。
