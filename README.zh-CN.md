# Double Gate（双重闸门）

[English](README.md) · [配置说明](docs/CONFIGURATION.md) · [安全政策](SECURITY.md) · [贡献指南](CONTRIBUTING.md)

[![CI](https://github.com/luohechentim/east-west-gate/actions/workflows/ci.yml/badge.svg)](https://github.com/luohechentim/east-west-gate/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

![Double Gate：面向 AI 辅助工作的证据化评审](docs/assets/social-preview.png)

**面向 AI 辅助工作流的、可追溯的评审面板、引用核验、质量闸门与修复闭环。**

Double Gate 是一个确定性的“双闸门”评审与修复工作流：先确认评审面板实际得出了什么结论，再核验工件与交付条件，最后由人决定是否发布。

### 30 秒理解

```text
提案 / 报告 / 代码变更
        │
        ├─ 闸门 1：面板评审 + 收敛裁决
        ├─ 闸门 2：引用工件守卫 + 六道交付闸门
        └─ 修复清单 → 独立复审 → 人工发布决定
```

内置离线面板是用于本地开发的确定性模拟，不是独立专家保证；真实多供应商面板只能提供供人判断的证据，不会证明结果一定正确。

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
git clone https://github.com/luohechentim/east-west-gate.git
cd east-west-gate
python -m pip install -e ".[dev]"
python examples/demo_offline.py

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

## 为什么值得使用

- **确定性结果：** 投票、证据状态和闸门结果都输出为明确 JSON，而不是只有一段解释性文字。
- **有边界的核验：** 引用路径只在指定的 `--root` 内检查，不探测任意本机路径。
- **修复是闭环：** 问题从一次回答中消失，不等于已经解决；还要看复审保证与复审结论。
- **适合 CI：** 无强制运行时依赖，提供稳定 CLI、离线夹具和机器可读报告。

如需引用或集成 Double Gate，请参阅 [CITATION.cff](CITATION.cff)；完整发布边界见 [docs/RELEASE_CHECKLIST.md](docs/RELEASE_CHECKLIST.md)。

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
