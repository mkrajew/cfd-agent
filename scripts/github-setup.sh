#!/usr/bin/env bash
# Idempotent GitHub repository setup: merge settings, labels, milestones, branch protection.
# Requires the GitHub CLI (`gh`), authenticated with admin rights on the repository.
# Safe to re-run: existing labels and milestones are updated, settings are re-applied.
set -euo pipefail

REPO="${REPO:-mkrajew/cfd-agent}"

echo "Repository: ${REPO}"

# --- Merge settings -------------------------------------------------------------------------
# Later PRs are stacked branches, and squash or rebase merges would break the stack.
gh repo edit "${REPO}" \
  --enable-merge-commit \
  --enable-squash-merge=false \
  --enable-rebase-merge=false \
  --delete-branch-on-merge

# --- Labels ---------------------------------------------------------------------------------
# `--force` updates the colour and description when the label already exists.
create_label() {
  local name="$1" color="$2" description="$3"
  gh label create "${name}" --repo "${REPO}" --color "${color}" --description "${description}" --force
}

create_label bug "d73a4a" "Something does not work as expected"
create_label enhancement "a2eeef" "New feature or improvement"
create_label infra "5319e7" "Azure, Bicep, Docker, CI"
create_label cfd "0e8a16" "CadQuery, Gmsh, OpenFOAM, post-processing"
create_label agents "1d76db" "LLM agents, orchestrator, prompts, providers"
create_label docs "0075ca" "Documentation"
create_label experiment "fbca04" "Benchmarks and experiment campaign"

# --- Milestones -----------------------------------------------------------------------------
# Create the milestone, or update its description when a milestone with that title exists.
ensure_milestone() {
  local title="$1" description="$2" number
  number="$(gh api "repos/${REPO}/milestones?state=all&per_page=100" --paginate \
    --jq ".[] | select(.title == \"${title}\") | .number" | head -n 1)"
  if [[ -n "${number}" ]]; then
    gh api --method PATCH "repos/${REPO}/milestones/${number}" \
      -f title="${title}" -f description="${description}" --silent
    echo "Milestone updated: ${title}"
  else
    gh api --method POST "repos/${REPO}/milestones" \
      -f title="${title}" -f description="${description}" --silent
    echo "Milestone created: ${title}"
  fi
}

ensure_milestone "M0 Foundations" \
  "Repo, CI, Docker image (OpenFOAM ESI + Gmsh + CadQuery + Python stack), Bicep infra, LLM capability spike (two models spiked, qwen3-32b excluded as not deployable), benchmark references and thresholds fixed. Done when cavity passes CadQuery > Gmsh > OpenFOAM > post-processing from hand-written scripts, no LLM, locally and on the VM."
ensure_milestone "M1 First autonomous run" \
  "Orchestrator, agents 1-6, CLI, Blob upload, run record. Done when cavity Re = 100 goes from text to results with no human intervention."
ensure_milestone "M2 Robustness + full benchmark set" \
  "Fix loops, Critic, Scorer, knowledge base; generality test set prepared (owner's task texts, references, criteria) but not run. Done when pipe and cylinder (Re 20, Re 100) pass."
ensure_milestone "M3 Experiment campaign" \
  "Frozen release; cfdagent bench; first runs of the generality test set; experiment matrix in design spec section 14.1. Done when statistics are ready for the report."
ensure_milestone "M4 Report" \
  "Report process in design spec section 11. Done when the PDF is approved by the owner."

# --- Branch protection on main --------------------------------------------------------------
# PR required with 0 approvals, required checks lint, infra-lint, unit, image, integration and
# sandbox (the job names in .github/workflows/ci.yml), no force push, no deletion.
# Run this after the last bootstrap push: afterwards main only changes via PRs, admins included.
gh api --method PUT "repos/${REPO}/branches/main/protection" --input - --silent <<'JSON'
{
  "required_status_checks": {
    "strict": false,
    "contexts": ["lint", "infra-lint", "unit", "image", "integration", "sandbox"]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 0
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
echo "Branch protection set on main"

echo "Done."
