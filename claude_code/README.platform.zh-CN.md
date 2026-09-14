# 在每轮创建新 ECS 的平台中运行

代码仓库保存构建定义和配置，不包含本机 Docker 镜像。平台需要在镜像发布阶段构建基础镜像，
在 ECS 初始化阶段准备生成镜像，再执行评测。`eval.sh` 不自动构建生成镜像；
评分镜像使用显式仓库地址时，在任务获得并发名额后的预检查中拉取并固定镜像 ID，
失败不会回退到本地标签；排队任务不提前拉取。

| 阶段 | 执行频率 | 操作 |
| --- | --- | --- |
| 基础镜像发布 | CLI/Python/系统依赖变更时 | 构建生成镜像，推送平台镜像仓库；登记各任务评分镜像 |
| ECS 初始化 | 每台新 ECS | 拉取本轮需要的生成镜像；评分镜像可选预热；可用预烘焙机器镜像保留 Docker 缓存 |
| 任务预检查 | 每个 task 获得并发名额后 | 按需拉取仓库评分镜像并固定 ID，检查 Python 主次版本；完整性检查按配置执行 |
| 每个 task 评分 | 每个生成产物 | 构建包含产物的安装验证镜像及官方评分镜像，运行并清理容器 |

生成镜像可在发布流水线中构建，例如从仓库根目录执行：

```bash
docker build -f claude_code/Dockerfile \
  --build-arg PYTHON_VERSION=3.12 --build-arg CLAUDE_CODE_VERSION=2.1.263 \
  -t nl2repo-claude-code:2.1.263 claude_code
docker build -f claude_code/Dockerfile \
  --build-arg PYTHON_VERSION=3.10 --build-arg CLAUDE_CODE_VERSION=2.1.263 \
  -t nl2repo-claude-code:2.1.263-py3.10 claude_code
```

将生成镜像发布到平台自己的 registry，并将配置中的 `generation_image` 换成完整仓库地址。
例如用 `registry.example.com/bench/claude-code:2.1.263-py3.10` 代替本地短名称。
上述域名为占位符；本仓库的修复不会自动推送镜像，也不包含 registry 凭据。
发布后推荐在平台配置中固定 registry digest，避免相同 tag 在不同 ECS 上指向不同内容。
构建参数中的 Python 主次版本 tag 本身会随补丁更新，固定已发布镜像 digest 才能复现相同环境。

在 ECS 初始化流程中先拉取平台配置引用的镜像，例如：

```bash
docker pull registry.example.com/bench/claude-code:2.1.263-py3.10
docker pull ghcr.io/multimodal-art-projection/nl2repobench/retrying:1.0
```

为所有选中任务准备生成镜像，去重共享的生成镜像；评分镜像可省略预拉取，由任务按需准备。
已有且未变化的镜像层会复用，同一 ECS
上的 task 共享镜像层；不会每个 task 都重新安装 Node 或 Claude Code。
评分镜像也可以提前镜像到平台内网仓库，同时修改 `grading_image`。这些准备动作在运行
候选代码前完成，任务容器本身继续使用断网隔离和受控 PyPI 依赖通道。

启动器的配置示例是本机配置。新 ECS 还需按照正常部署流程安装宿主机 Python 依赖、
Docker、LiteLLM，并通过平台密钥管理注入模型/网关配置，不能依赖本机 `.nl2repo-local`
未纳入版本控制的文件。

运行时先把配置引用解析为本机不可变 ID，并记录生成/评分 Python 完整版本。Python 主次版本
不一致或基础镜像缺失会在开始模型工作前报错。此检查与是否开启镜像残留检查相互独立。
运行中的 Python 版本探针按不可变镜像 ID 缓存，同一进程中共享生成镜像不重复探测。
开启残留检查时仍需遵守 reviewed 与本机不可变 ID 的登记规则。

每个 task 的两个派生镜像主要增加生成代码及少量运行配置，基础层可以复用。当前实现仍有
这一步 Docker build 开销；派生镜像留在当前 ECS 的 Docker 存储中，随 ECS 回收清除。
运行容器在正常结束、安装失败和超时路径上都会清理；实验工作区、ZIP、原始/readable 轨迹、
结果和日志应由平台在 ECS 回收前上传到持久存储。

安装时间预算默认 600 秒，官方构建/安装/测试预算默认 1800 秒，可用
`--install-timeout-seconds` 和 `--grading-timeout-seconds` 调整。模型生成仍使用独立的
`--timeout-seconds`。平台应同时查看退出码、`evaluation_valid`、`failure_stage` 和 skip 覆盖信息，
不能只读取 `score`；部分产物可能有诊断分数，但生成或安装并未成功。

产物安装检查支持在 `claude_code.offline_environments.<任务名>` 中配置
`"artifact_install_commands": ["python install.py"]`。未配置时仍使用
`python -m pip install -e .`；默认配置中的 `autojump` 使用 `python install.py`。
列表必须非空，每项必须是非空命令字符串，启动器会在启动实验前验证。
命令来自评测配置，在产物副本中执行；它们不替换官方评分的安装及测试命令。
多条命令共享安装时间预算，前一条失败后不再执行后续命令。

任务生成超时记录为 `failure_kind=generation_timeout`、`failure_stage=generation`。
CLI 的 `terminal_reason=api_error` 即使没有 HTTP 状态码，也记录为
`upstream_api_error`。生成失败后如果评分也失败，主失败原因仍保留生成错误，
后续问题记录在 `evaluation_failures`；评分内部各阶段的问题保存在
`post_process_result.failures`，包含 `kind` 和 `stage`。

评分容器在清理前保存 `container_diagnostics`，包含 Docker 的退出码、运行状态、
`OOMKilled`、启动/结束时间和最近 200 行容器日志（最多保留 65,536 字符）。
诊断位于 `post_process_result.artifact_install` 或 `test_results` 中；
容器创建阶段抛出异常时位于 `post_process_result` 下，并同步写入评分日志。
容器主进程提前退出归类为 `environment_error`，不会当作普通测试断言失败。
诊断采集失败会记录采集错误，并继续清理容器，不覆盖已有测试结果。
容器辅助脚本支持评分镜像中的 Python 3.7；这不替代生成/评分版本一致性检查。
