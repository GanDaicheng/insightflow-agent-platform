# InsightFlow System Architecture Diagram Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Create a source-controlled technical architecture document that accurately explains InsightFlow's current components, data flows, deployment topology, and Demo/Real boundaries.

**Architecture:** Add one Markdown document containing Mermaid diagrams for the overall platform, the three implemented Agent workflows, and Docker Compose deployment. Link it from the root README without changing runtime code or duplicating claims that are not present in the repository.

**Tech Stack:** Markdown, Mermaid, existing FastAPI/LangGraph/Deep Agents/RAG/PostgreSQL/pgvector/Next.js/Docker Compose terminology.

**Spec:** `docs/superpowers/specs/2026-09-27-system-architecture-diagram-design.md`

## Global Constraints

- Base every current-state node and edge on the repository's implemented behavior.
- Do not present Kubernetes, HPA, enterprise SSO, multi-tenancy, production monitoring, or HA database as current capabilities.
- Do not include API keys, passwords, full connection strings, or other secrets.
- Preserve existing runtime code, API response shapes, database data, and Docker behavior.
- Keep Demo mode and Real mode explicitly distinct.

## Review Focus

- Diagram claims a component exists when it is only planned: validate every current-state node against README, architecture data, Docker Compose, or implementation paths.
- Workflow order disagrees with runtime behavior: validate the three Mermaid sequences against the existing README and Agent implementation descriptions.
- Deployment diagram omits init or readiness: assert both are present and the dependency order is visible.
- Demo mode is described as real model execution: assert the document says Demo avoids external LLM, Embedding, and Reranker calls.
- Mermaid syntax or Markdown links are broken: render or parse-check the document and verify the README link target.

### Task 1: Create the technical architecture document

**Files:**
- Create: `docs/architecture.md`
- Read: `README.md`, `frontend/src/features/architecture/architecture-data.ts`, `docker-compose.yml`, `backend/app/agent/data_query/graph.py`, `backend/app/agent/business_analysis/agent.py`, `backend/app/services/readiness.py`

**Interfaces:**
- Consumes: current repository architecture names, workflow order, service names, runtime mode behavior, and readiness dependency.
- Produces: `docs/architecture.md` with six sections: overview, system diagram, three workflow diagrams, deployment diagram, boundaries/operations.

- [ ] **Step 1: Write the document outline and current-state component inventory**

  Include only these current layers: user/interface, API/orchestration, Agent capabilities, data/retrieval, persistence/infrastructure. Include Next.js, FastAPI, LangGraph, Deep Agents, RAG services, safe query, PostgreSQL/pgvector, Checkpoint/Store, SSE, init, and readiness where supported by the repository.

- [ ] **Step 2: Add the overall Mermaid architecture diagram**

  Show the path `User → Next.js Web 工作台 → FastAPI API → domain routing → Agent/services → PostgreSQL + pgvector`, with external model services shown as dependencies of Real mode only. Label Demo mode as deterministic providers rather than a model service.

- [ ] **Step 3: Add the three workflow Mermaid diagrams**

  Add exact sequences for:
  - data query: intent/assets → SQL generation → AST validation → bounded repair → read-only execution → explanation/chart;
  - RAG: document ingestion/retrieval → query rewrite → vector/keyword recall → RRF → rerank → sourced answer;
  - business analysis: Deep Agent → data query/RAG/metric/report tools → Checkpoint/Store → SSE/report.

- [ ] **Step 4: Add the Docker Compose deployment diagram**

  Show `postgres healthy → init completed successfully → backend readiness → frontend healthy`. Include the persistent PostgreSQL/pgvector volume and distinguish `health` liveness from `readiness` service readiness.

- [ ] **Step 5: Add boundaries and operating notes**

  Document Demo/Real behavior, the security boundary that prevents direct Agent database access, the safe-query and read-only transaction boundary, and the current versus future capability list from the approved spec.

- [ ] **Step 6: Render/check the Markdown and Mermaid content**

  Verify all code fences close, Mermaid blocks use supported syntax, headings are in the expected order, and no secret-like values or full connection strings appear.

- [ ] **Step 7: Commit the document**

  ```powershell
  git add docs/architecture.md
  git commit -m "docs: add system architecture diagrams"
  ```

### Task 2: Link the architecture document from the README

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: `docs/architecture.md` from Task 1.
- Produces: one stable relative link in the README's documentation section.

- [ ] **Step 1: Add the documentation link**

  Add `docs/architecture.md` to the existing detailed-documentation list without rewriting unrelated README sections.

- [ ] **Step 2: Verify the link and scope**

  Confirm the target exists, the link is relative, and the diff contains no runtime or configuration changes.

- [ ] **Step 3: Commit the README link**

  ```powershell
  git add README.md
  git commit -m "docs: link architecture guide"
  ```

### Task 3: Final documentation verification

**Files:**
- Test: `docs/architecture.md`, `README.md`

- [ ] **Step 1: Run repository checks relevant to documentation**

  Run:

  ```powershell
  git diff main...HEAD --check
  rg -n "API_KEY|password=|postgresql\+|postgresql://|sk-[A-Za-z0-9]" docs/architecture.md README.md
  git status --short
  ```

  Expected: no whitespace errors, no secret-like values, and only the pre-existing `?? start-docker.bat` remains untracked.

- [ ] **Step 2: Review each architecture claim against the implementation**

  Check that the document includes all six deliverables from the spec and does not claim Kubernetes or production HA as implemented.

- [ ] **Step 3: Report the final artifact**

  Provide the absolute file link, a short list of diagrams included, the verification results, and the final commit hashes.
