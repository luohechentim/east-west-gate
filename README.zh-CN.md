# Double Gate（双重闸门）

[English](README.md) · [配置说明](docs/CONFIGURATION.md) · [安全政策](SECURITY.md) · [贡献指南](CONTRIBUTING.md)

**面向 AI 辅助工作流的、可追溯的评审面板、引用核验、质量闸门与修复闭环。**

Double Gate 的目标不是“再让一个模型说一遍”，而是把评审结论拆成可检查的证据：谁评审了、哪些结论收敛、引用的文件/API/符号是否真的存在、哪些问题还必须由人决定。

## 能做什么

- 并行、盲审式地调用多个审稿席位，并统一解析 JSON 结果；
- 用确定性规则裁决结论，明确区分 `unanimous`、`majority`、`split` 和 `single`；
- 对**审稿人输出中引用的**文件、API、导入、符号和字段做五层核验；
- 对任务、数据、评审、偏差、输出、交付信息运行六道确定性质量闸门；
- 将问题转成修复清单，并把“复审未再提及”和“有足够证据确认已解决”区分开。

## 关键边界

内置 `stub` 是离线演示与测试夹具，**不是独立专家评审**。因此其结果会标为 `offline-simulation`，交付状态必然要求人工复核。即使配置了真实多供应商面板，Double Gate 也只提供评审证据，不会替代负责人对代码、设计或报告的最终批准。

幻觉守卫核验的是审稿人提到的工件是否在指定根目录下存在；它不等于验证全部事实，更不会扫描根目录之外的任意本机路径。

## 安装与离线试跑

```bash
python -m pip install -e ".[dev]"

double-gate review proposal.md --risk low --offline --root .
double-gate gates proposal.md --risk low --context examples/gates-context.json --fail-on-failure
double-gate guard reviewer-output.md --root ./your-repository
```

所有命令向标准输出写 JSON，便于接入 CI。`review` 可以使用 `--fail-on serious` 或 `--fail-on any` 显式控制非零退出码。

## 真实评审面板

通过 `DOUBLE_GATE_PANELS_JSON` 配置真实面板。每个成员可单独指定 OpenAI 兼容端点、`api_key_env`、模型、角色和 `provider` 独立性分组。密钥只放环境变量，绝不放仓库或 JSON 配置。完整示例见 [docs/CONFIGURATION.md](docs/CONFIGURATION.md)。

```bash
double-gate review proposal.md --risk high --panel deep --root ./your-repository
```

只有真实、互相独立的供应商才应使用不同的 `provider` 分组；单一供应商会被标为 `single-provider`。

## 编程调用

```python
from double_gate import jury, repair

first = jury.submit(
    "需要评审的设计或代码说明",
    submission_type="design",
    risk_level="high",
    codebase_root="./your-repository",
    offline=True,  # 仅用于流程验证
)

checklist = repair.build_checklist(first)
# 完成修改后，用真实多供应商面板获得新的复审结果。
second = jury.submit("修改后的内容", risk_level="high", codebase_root="./your-repository")
report = repair.verify_fixed(second, checklist)
```

`all_findings_absent` 只表示旧问题没有在复审中再次出现；`all_resolved` 还要求足够的独立复审保证和可接受的复审结论。

## 开源发布前

在上传 GitHub 前请运行：

```bash
python -m ruff check .
python -m pytest -q
python -m build
python -m twine check dist/*
```

并按照 [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md) 检查密钥、客户数据、GitHub 分支保护和私密漏洞报告设置。

## 许可证

[MIT](LICENSE) © 2026 Double Gate contributors。
