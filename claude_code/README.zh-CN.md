# Claude Code 运行环境与实现

日常启动、参数、日志和停止方法统一见[评测使用说明](../eval.README.zh-CN.md)。
本目录实现 Claude Code 生成流程；评分由独立的 `grading/` 模块负责，
通过 `docker_self/` 操作评分容器。`python main.py` 默认读取根目录
`config.claude_code.json`。

## 运行环境

运行环境使用 Python 3.12，依赖版本记录在根目录 `requirements.txt`。
Phoenix 的私有模板和凭据保存在 `.nl2repo-local/`，目录不提交到 Git。

新机器可准备独立 Python 3.12 环境并安装运行依赖：

```bash
python3.12 -m venv .nl2repo-local/venv
.nl2repo-local/venv/bin/pip install -r requirements.txt
```

Phoenix 模式还需要配置 `.nl2repo-local/litellm.phoenix.yaml`。
文件内容使用 JSON（同时是合法 YAML），例如：

```json
{
  "model_list": [{
    "model_name": "repo-model",
    "litellm_params": {
      "model": "openai/default",
      "api_base": "http://phoenix-gw-eval.alibaba.com/eval/v1",
      "api_key": "dummy",
      "timeout": 3600,
      "extra_headers": {
        "x-eval-token": "os.environ/PHOENIX_EVAL_TOKEN",
        "x-eval-domain-proxy": "http://MODEL_HOST:PORT",
        "X-Backend-TrajectoryID": "proxy_MODEL_HOST:PORT"
      }
    }
  }]
}
```

占位地址需替换，令牌通过环境变量或私有配置提供；私有文件权限设为 600。
已有私有配置可继续使用。启动器自动生成转换设置和每次实验的网关密钥。

## Claude Code 容器

固定 CLI 版本并构建镜像，再将任务环境登记到 `config.claude_code.json`：

```bash
docker build -f claude_code/Dockerfile \
  --build-arg CLAUDE_CODE_VERSION=2.1.263 \
  -t nl2repo-claude-code:2.1.263 .
```

生成镜像提供 Python、Node 和 Claude Code，按非 root 用户运行。
生成与评分使用 `network=none`，模型和受控 PyPI 安装通过独立 Unix socket 通道访问。
不向任务容器挂载 Docker socket、宿主机 home 或整个仓库。
每个任务需要的生成镜像、评分镜像、目标包和模块名单位于
`claude_code.offline_environments`；具体要求见[完整性说明](README.integrity.zh-CN.md)。

`baseUrl` 由宿主机模型通道访问，所以自动创建的网关只监听 `127.0.0.1` 即可。
容器先连接自身的回环中继，再由 Unix socket 到达宿主机。

## 模块职责

| 文件 | 职责 |
|---|---|
| `launch.py` | 参数、预检、实验配置、后台监督、网关与评测生命周期 |
| `phoenix_config.py` | Phoenix 路由请求头、环境变量覆盖、私有配置处理 |
| `phoenix_adapter.py` | system 消息归并及 Anthropic 流错误适配 |
| `generation.py` | 生成参数校验及模板参数透传规则 |
| `diagnostics.py` | 上游请求参数、结束原因和用量的脱敏诊断 |
| `runner.py` | 任务容器、代码生成、评分及结果保存 |
| `model_channel.py`、`container_relay.cjs` | 容器到宿主机的受限模型通道 |
| `offline.py` | 任务环境检查与评分环境连接 |
| `dependency_channel.py`、`package_relay.py` | 受控 Python wheel 安装通道 |
| `integrity_hook.py` | 提前拦截获取目标项目实现的操作 |
| `trajectory.py` | 完整对话轨迹保存和历史轨迹转换 |

## 协议兼容与评测约定

Claude Code 使用 Anthropic Messages API；Phoenix 转发的 SGLang 服务使用
OpenAI Chat Completions。LiteLLM 设置
`use_chat_completions_url_for_anthropic_messages: true`，固定采用 Chat Completions 转换。
模型别名为 `repo-model`，上游实际模型默认继承 Phoenix 模板，可以用 `--model` 覆盖。

`PhoenixAdapter` 将消息列表里的 system 内容并入顶层 system，保留内容和其余消息顺序，
以适配 SGLang 模板。网关在 LiteLLM 补结束标记前检查提供方 finish_reason，
无结束证据的 EOF 和不完整工具参数会补发 Anthropic `event: error`。
工具参数按 index/id 聚合后分别输出，支持同一流块及交错的多工具调用。
原始提供方结束原因和用量单独记入 gateway.requests.jsonl 的 provider_stream_end，
不将 LiteLLM 的聚合/估算值标为提供方实际用量。计数请求转发配置的鉴权和路由头，
使用不超过 30 秒的独立超时，失败时明确回退本地估算。
runner 同时检查 CLI 退出码、错误事件及最终结束原因；CLI 正常退出不一定代表生成成功。
正式对比需记录这项提示位置调整、CLI/网关/模型版本、工具集合和预算。

网关启动前设置 TCP keepalive，默认 idle=30、interval=15、count=4。
TCP 探测是否解决长时间静默，取决于上游连接和代理超时机制，应由真实长请求验证。
未部署的上游 SSE 心跳补丁已移除。

默认工具集合为 `Bash,Read,Write,Edit,Glob,Grep`，关闭客户端 prompt caching 和额外 thinking 预算。
这不表示上游模型已关闭思考；上游模板参数须单独配置。
Claude Code 容器默认接收以下请求超时、流式 watchdog 和重试配置：

```bash
export API_TIMEOUT_MS=1800000
export API_FORCE_IDLE_TIMEOUT=0
export CLAUDE_ENABLE_STREAM_WATCHDOG=1
export CLAUDE_STREAM_IDLE_TIMEOUT_MS=1800000
export CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS=1800000
export CLAUDE_CODE_MAX_RETRIES=1
```

无需手动 export 即使用上述默认值；在启动评测前 export 同名变量可覆盖默认值。
这些变量显式传入 Docker，并记录在任务结果的 `claude_code_environment` 中。
变量是否被识别及具体行为取决于容器内 Claude Code 版本，尤其是
`CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS`；传入变量不等于已验证客户端支持。
这些配置与 `--model-timeout-seconds` 和 `--timeout-seconds` 分别控制不同层的限制。
`--generation-retries` 仅允许生成开始前的环境准备重试，默认最多额外一次。
API/流错误由客户端在原会话内重试当前请求；当前 2.1.263 镜像已验证 503 重试以及
流中断后的非流式恢复均保留消息上下文。客户端最终退出后保留已有代码和轨迹、记录失败，
任务结束后不会自动恢复会话或重新生成。
准备、生成与重试等待共享 `--generation-budget-seconds`（默认等于 `--timeout-seconds`）；
客户端请求级重试也消耗共享预算。评分使用独立预算。
修改后需启动新的评测进程，已有进程及其排队任务不会热更新。

结果应标注为模型 + Claude Code；harness 会影响提示词、上下文管理和工具策略。
生成失败但已退出的任务仍对已有代码评分，超时任务停止容器并记录错误。
最终评分通过数大于 0 时，顶层 `score_valid=true`，即使任务为 `failed` 或评分有故障。
已有的有效 0 分仍有效；`evaluation_valid` 和失败状态独立保留，用于诊断执行情况。
结束后自动写入 `report.md`、`report.json`、`report.csv`，主分数为所有配置任务的
等权平均分，有正分即计入，无可用分数/未评测记 0。全量任务和官方分母在启动时
保存到 `task-plan.json`。历史实验可用 `python -m claude_code.report 实验目录` 补生成。

历史原始轨迹可转换，原文件会保留：

```bash
.nl2repo-local/venv/bin/python -m claude_code.trajectory /path/to/trajectory.jsonl
```

添加 `--watch` 可持续刷新。历史转换默认写入同目录的 `trajectory.json`。

新评测只保存一份 **`trajectory.json`**（schema_version=2、UTF-8、两空格缩进）。
原始 CLI JSONL 通过管道直接解析，不写原始文件或临时原始文件。
思考、正文、工具调用及结果按消息/内容块组织；同一消息跨工具结果的片段只合并一次，
用量也只记一次。流尚未结束的内容持续可见，以 stream_complete/complete 标识；
残缺参数保存在 partial_input，usage 未到达时为 null，不写虚假的零用量。
progress 保存阶段、最近活动与内容时间；estimated_thinking_tokens 明确是客户端估计。

每两秒原子替换一次 JSON 快照，正常退出、超时和可协调的中断会排空管道并最后保存。
SIGKILL/断电只能保留最近快照，最多可能缺少最近一次保存后的内容。
任务开始前在任务目录的 `task_state.json` 写 queued 状态，运行期间更新阶段，
中断后保留 interrupted 终态；supervisor 对强制停止后仍未完成的任务补状态。
`result/` 仅在评测结束后写最终结果，未进入评测的失败或中断只写任务状态和轨迹。
`completed` 表示流程完成；生成代码的断言、导入/收集或安装错误不作为评测 infra 故障。
Docker、pytest 内部错误、执行超时等仍保留评测失败。评分、stderr 和工作区文件分别保存。

## 验证

```bash
PYTHONDONTWRITEBYTECODE=1 LITELLM_LOCAL_MODEL_COST_MAP=True \
  .nl2repo-local/venv/bin/python -m unittest discover -s tests -v
```

默认运行本地回归测试，部分测试会启动本机模拟 HTTP/Unix socket 服务。
设置 `NL2REPO_TEST_DOCKER=1` 可额外运行需要真实 Docker 和 CLI 镜像的集成测试。
普通单元测试不验证 Phoenix 上游连通性或真实模型生成能力。
