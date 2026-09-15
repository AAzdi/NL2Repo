# H100-all-002 评测修复

## H100-all-005 增补（2026-09-15）

当前修复版本为 `h100-grading-20260915-v2`，包含下面的增补及原有修复。

### 部分成绩计入（按后续用户要求）

只要评测记录中有正分就计入，即使命令超时、任务失败或缺少最终汇总。解析器现在可从有会话/收集标题的 pytest 标准进度中恢复已完成结果，支持紧凑进度及带 node ID 的详细进度；完整汇总优先，详细进度按 node ID 去重，忽略失败详情，拒绝无法消歧的重绘或嵌套会话。不会直接统计任意输出中的点号。

恢复结果写入 `pytest_results.partial_progress`；`summary_complete`、嵌套 `score_valid` 与执行故障仍如实记录，runner 已有的正分规则负责将通过数计入顶层成绩。重试不累加。修复后的官方测试结果覆盖旧的错误测试范围；pytz 旧输出含候选自测，不能从中恢复成绩。

H100-all-005 新增恢复 boltons=138、cachier=50、databases=30、tenacity=13，共 231 项；合并此前四项修复补评后，全量 104 项平均为 **42.5379%**，通过数 **6385**。本轮没有重跑模型或测试。

- `autorccar`：原始参考镜像没有 setup.py/pyproject.toml，参考测试阶段直接运行 `test`。候选产物仍由独立 artifact_install 阶段验证安装。
- `box`：参考安装脚本使用已有的纯 Python 回退路径，避免因为评测镜像预装 Cython 就强制编译候选 Python 源码。
- `mechanicalsoup`：参考安装脚本依赖候选不必提供的描述元数据，且原镜像缺少其引用的 requirements.txt；参考阶段直接运行 `tests`，候选安装仍单独检查。
- `python-pytest-cases`：参考隔离构建固定 `setuptools==80.9.0`，保留 setup.py 需要的 pkg_resources。
- `pytz`：只执行参考镜像的 `test_docs.py test_lazy.py test_tzinfo.py`，排除候选自带 tests。显式设置 `/workspace/src:/workspace` 并检查 pytz 来源，避免加载镜像残留的官方包。

H100-all-005 原候选的独立参考评测验证得到：autorccar 6/13、box 60/147、mechanicalsoup 83/121、pytz 3/235。python-pytest-cases 隔离安装成功，但候选随后因导入不存在的 pytest 私有 API 而失败。没有修改候选实现或覆盖历史成绩。

### 推送后在新机器上运行

修复保存在仓库源码中，不依赖本机临时容器、Docker commit、验证镜像或实验目录里的 wheel 缓存。请一并提交：

- `grading/reference_repairs.py`、`grading/post_processor.py`
- `test_files/autorccar/test_commands.json`
- `test_files/mechanicalsoup/test_commands.json`
- `test_files/pytz/test_commands.json`
- `tests/test_grading_repairs.py` 和本文档

正常启动评测时，`test_data_service.py` 从仓库的 `test_files/` 读取命令；`post_process_task` 每次构建派生镜像时复制并执行仓库中的 reference_repairs.py，然后才复制候选源码。因此，新机器拉取基础镜像后会自动重新应用修复，无需手工修改或重新发布基础镜像。需要正常的依赖代理访问来取得固定构建工具版本；本地验证 wheel 只用于断网验证。

检查新结果的 `post_process_result.reference_repairs_revision` 是否为 `h100-grading-20260915-v2`，并检查构建日志的 `reference_repairs.files` 和执行命令，即可确认生效。已有结果不会自动重算，须重新评测。若同名基础镜像未来改变参考文件布局，严格匹配修复可能报错，需要适配新镜像；修复可迁移不代表任意镜像版本或新候选都能得到同一分数。

---

修复版本：`h100-grading-20260911-v1`。

本次修复只改评测工具和参考环境，不修改 agent 生成的实现，也不覆盖历史结果。参考文件修复在派生评测镜像中、复制候选源码之前执行，原始镜像不变。新结果记录 `reference_repairs_revision`，构建日志记录具体修改文件。

## 修复内容

- `binaryalert`：通过 `env AWS_DEFAULT_REGION=us-east-1 pytest ...` 在同一进程传递环境变量。
- `parse`：将包含 `&` 的安装命令拆成两个顺序命令，失败时停止后续步骤。参考命令按 argv 执行；显式 shell 命令仍需 `sh -c`。
- `sklearn` 等：ZIP 保存符号链接本身，不在主机解引用容器路径，也不读入链接指向的外部文件。
- `pyautogui`：识别 `xvfb-run`、`env` 包装的 pytest 命令；安装 pytest 或 Python 字符串中提到 pytest 不会被误认。
- `pytest-cov`：保留外层首次收集数量，不用失败详情中的嵌套 pytest 计数或 INTERNALERROR 覆盖外层结果。
- conftest 缺失候选模块/API：由可信 `target_modules` 和错误文本共同确认后，记录候选导入失败/零分，而不是仅凭 pytest 退出码 4 判框架故障。缺失第三方插件仍不自动归给候选。
- `pylama`：参考构建环境固定 `setuptools==80.9.0`，保留旧安装脚本需要的 `pkg_resources`。
- `tenacity`：对缺失项目元数据的参考打包配置补充明确包发现规则；版本读取原始镜像已安装分发的 METADATA，不读取候选代码。完整参考打包配置不覆盖。
- `deslib`：参考测试显式导入 numpy；测试辅助函数从参考 `tests` 导入，不依赖候选自带测试。
- `pathlib2`：将参考测试清理辅助函数的权限重试操作保留在本文件，消除对镜像缺失的 CPython `test.support` 的依赖。
- `requests-html`：5 组外部网址改为 loopback HTTP 页面，仍运行原来的 11 个抓取、同步/异步分页和异步批量请求测试，保留断言。此项改变了测试输入环境，跨版本成绩对比应注明修复版本。
- 依赖代理：pip/容器 relay 等待时间协调为 600 秒；索引缓存 300 秒并合并同包并发请求；wheel 压缩大小上限 2 GiB、展开大小上限 8 GiB，流式落临时文件，完整校验哈希、包身份、成员路径、目标模块和依赖后才发送。元数据限制 16 MiB。没有开放目标分发、目标模块、任意 URL 或 sdist。
- 依赖网络故障返回 502，客户端断开单独记日志；不再将所有故障统一描述为策略拒绝。OOM 记录 `failure_reason=oom_killed` 并使该次测试成绩无效；这不推断 OOM 是候选泄漏还是机器资源不足。

## 已做验证

8 个原始候选副本用固定原始评测镜像实际运行参考安装和测试，均得到有效评测结果：

| 项目 | passed | failed | errors |
|---|---:|---:|---:|
| binaryalert | 9 | 48 | 1 |
| parse | 48 | 48 | 0 |
| pylama | 13 | 21 | 16 |
| tenacity | 63 | 61 | 0 |
| deslib | 149 | 383 | 0 |
| pathlib2 | 282 | 62 | 0 |
| requests-html | 11 | 0 | 1 |
| pyautogui | 12 | 16 | 0 |

这些是参考评测阶段验证，本轮没有对这 8 项重新运行模型生成或候选自有打包安装阶段，不应直接据此重算整个实验的端到端成绩。errors 中的候选 API/fixture 错误仍保留，评测有效不等于测试全通过。

此外，原始 `pytest-cov` 日志重解析得到 140 passed、44 failed、27 skipped、collected=211；原始 sklearn 候选归档成功；真实 numpy 索引和缓存验证通过；270 MiB 本地测试 wheel 流式校验通过；Docker 验证无关依赖可安装、目标包及别名和传递依赖仍受阻。

验证结果独立保存于 `/root/nl2repo-grading-validation`。回归测试见 `tests/test_grading_repairs.py`、`tests/test_dependency_channel.py`、`tests/test_grading_lifecycle.py`、`tests/test_pytest_results.py`。

## 后续重评边界

`cherry` 的依赖传输机制已修复，但应重跑该候选的完整安装及评测，确认第三方依赖组合。PyTorch 大 wheel 通道已修复，原先的运行时 OOM 仍需结合内存资源和逐例日志定位；此修复没有无依据地提高机器内存限制。`databases` 的测试阻塞、`paillier` 的 pycrypto 可用性仍是前次审计列出的待定位项。历史 H100-all-002 结果保持不变。
