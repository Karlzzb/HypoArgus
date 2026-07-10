# HypoArgus

论证驱动型文档修订多智能体系统。本文件只承载 Agent skills 配置；项目规范与术语见 `CONTEXT.md`，架构决策见 `docs/adr/`，完整需求见 `prd_v2.0.md`。

## Agent skills

### Issue tracker

Issues live as GitHub issues in `Karlzzb/HypoArgus` (use `gh`); external PRs are **not** a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical roles map 1:1 to GitHub labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: `CONTEXT.md` at the repo root is the ubiquitous-language glossary; `docs/adr/` holds architectural decisions. Skills read these before exploring. See `docs/agents/domain.md`.
