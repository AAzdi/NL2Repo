# Hook 与受控依赖安装

当前 Claude Code 使用 `hooks-controlled-pypi-v1`，取代上一版只能使用预装依赖的
`offline-v1`。**不需要提前打包所有依赖**：模型可以执行普通的
`python -m pip install PACKAGE`，由受控通道按需提供第三方 wheel。

## 三层防护

1. 提示词明确禁止获取原项目实现及原测试，包括 Git、包发布物、镜像站、缓存、
   vendored 副本，以及编写代码在生成或评分阶段延迟下载。目标包出现在任务依赖列表中，
   也不代表允许安装原实现。同时明确允许无关第三方依赖和本地项目安装。
2. Claude Code 的 `PreToolUse` Hook 检查 Bash、Write、Edit，提前拒绝可识别的目标包
   安装和目标源码下载逻辑，并向模型解释原因。Hook 脚本、目标规则和 managed settings
   都只读挂载；使用绝对路径解释器及 `-I`，减少 Python 路径覆盖的影响。
3. 生成和评分容器仍使用 `network=none`、移除 capabilities、禁止提权。模型自行执行
   Python/Node 下载、修改 pip 源、直连 IP，都没有外网路由。模型 API 与包安装各有独立
   Unix socket 通道；评分阶段只挂载包通道，不能使用模型通道。

Hook 是提前反馈机制，不能静态识别所有脚本行为。实际联网边界由容器隔离和宿主机
通道执行；不会因为 Hook 漏检就开放容器网络。

Hook 按引号和命令分隔符拆分 Bash，再检查 pip 的实际安装参数；本地路径、
日志/输出目录、后续测试命令和注释不会作为目标包安装处理。例如
`pip install /workspace && python retrying.py` 和本地 wheel 安装均允许。
目标包的版本约束、extras、规范化别名、远程下载，以及 Python 中可静态识别的
subprocess/网络调用仍会拒绝，拒绝消息说明命中的目标包。
Python 文件使用语法树检查调用，避免把普通字符串、注释和 docstring 当成下载代码。
这是尽力识别机制，不执行候选代码、不读取候选路径；动态表达式、复杂 shell 语法、
不完整 Python 编辑和其他语言不能保证被 Hook 识别，仍由网络隔离和受控包通道约束。

每个任务启动时会复制一份 Hook 到任务的 `integrity/hook.py` 并只读挂载。
修改仓库中的 Hook 会应用于后续启动的任务；已经启动的任务保留原快照，便于复现实验。
[Claude Code Hook 文档](https://code.claude.com/docs/en/hooks#pretooluse-decision-control)、
[Docker none 网络](https://docs.docker.com/engine/network/drivers/none/)。

## 安装通道

- 宿主机只查询固定的 `https://pypi.org/simple/<包名>/`，按规范归一化包名并拒绝目标包
  及别名。例如 boto 对应 boto3，box 对应 python-box。pip 解析传递依赖时也须经过同一检查。
- 仅提供 wheel。文件地址必须来自 PyPI 元数据且位于 `files.pythonhosted.org`，拒绝重定向。
  下载后校验 SHA-256、METADATA 包身份、归档路径和大小，并拒绝直接 URL 依赖。
- 额外检查顶层目标模块以及常见 vendor/_vendor/vendored 目录中的目标模块副本。
  不把其他包内部恰好同名的普通文件直接认定为原实现。
- 容器只收到本任务索引地址；不能指定宿主机下载任意 URL、读取任意宿主机路径，
  也不能让宿主机执行候选 setup 脚本。第三方代码仅在隔离容器中安装和执行。
- 每次索引访问、wheel 哈希、传输和拒绝记录分别保存为任务目录中的
  `dependencies.generation.jsonl`、`dependencies.grading.jsonl`。Hook 拒绝原因进入轨迹。
  拒绝事件是审计线索，不自动等同于模型蓄意作弊。

首版限制：不提供 sdist、Git URL、自定义包源或 apt/npm 等系统/非 Python 安装服务。
单个 wheel 上限 256 MiB，解压后总大小上限 1 GiB。需要这些能力时应由实验准备者
补充经过检查的环境或扩展受控安装器；不会自动放开外网。

本地项目安装如 `pip install -e .` 允许执行，其构建依赖也可从受控索引获取。
评分阶段根据项目声明的依赖安装；模型仍应正确声明运行依赖。

## 基础镜像登记

`claude_code.offline_environments` 是历史字段名，现在登记的是**经过检查的基础镜像**，
不再表示必须预装所有依赖。生成镜像提供 Python、Node、Claude Code；评分镜像提供
Python、pytest、权威测试和必要测试配置。二者不能残留目标源码、可复原实现的归档或缓存。
每个任务获得并发名额后执行基础环境预检，通过后才允许该任务开始生成；
不等待其他任务预检完成，单个任务预检失败不阻止其他任务执行。

本次全量实验按用户要求设置 `claude_code.check_image_residue: false`：跳过残留扫描和
人工检查声明要求，允许现有镜像标签，评分基础镜像按需拉取。目标包/模块名单仍必填，
Hook、受控 PyPI 通道和任务网络隔离保持启用。结果中的
`offline_environment.image_residue_check` 记录为 `skipped`，不能解读为镜像已通过源码检查。
未设置此字段时仍默认启用检查。

```json
{
  "offline_environments": {
    "aiofiles": {
      "reviewed": true,
      "generation_image": "sha256:<64位本地镜像ID>",
      "grading_image": "sha256:<64位本地镜像ID>",
      "target_distributions": ["aiofiles"],
      "target_modules": ["aiofiles"]
    }
  }
}
```

占位符不能直接运行。`reviewed` 表示准备者检查过镜像内容。框架另外检查不可变 ID、
ONBUILD/隐式卷、目标 distribution/顶层模块和评分器 pytest。仅卸载包不足以证明镜像干净；
例如旧 six 镜像中的 pip 还包含 vendored six，需要额外处理。

`baseUrl` 由宿主机访问；历史 `http://host.docker.internal:4000` 自动映射到
`http://127.0.0.1:4000`，也可通过 `claude_code.host_base_url` 指定。
Phoenix 网关到上游的 TCP keepalive 修复继续生效。

## 验证与边界

```bash
env PYTHONDONTWRITEBYTECODE=1 NL2REPO_TEST_DOCKER=1 LITELLM_LOCAL_MODEL_COST_MAP=True \
  .nl2repo-local/venv/bin/python -m unittest discover -s tests -v
```

集成测试使用真实 Claude Code 和 Docker、模拟模型/包源，覆盖 Hook 拒绝后继续工作、
正常包安装、直接/别名/传递目标包拒绝、评分网络隔离，以及已有网关和轨迹回归。
真实 PyPI 安装验证记录在
`experiments/phoenix-offline-smoke-001/environment/real-pypi.result.json`。

本策略不证明训练数据没有代码记忆，也不能识别任意改名、混淆后藏入其他 PyPI 包的原实现。
因此不能宣称消除了所有形式的作弊；仍需基础镜像检查、包来源信任与结果审计。
此实现覆盖 Claude Code 生成流程及 `grading/` 评分流程。
旧实验已确认复制原实现的成绩应单独标识，不与新协议下的独立实现成绩混算。
