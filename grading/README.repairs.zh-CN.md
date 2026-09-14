# H100-all-002 评测修复

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
