#!/usr/bin/env bash
# Build uploadable skill zips in dist/ and install the Claude skill for Claude Code.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p ../dist
for target in claude gpt; do
  rm -f "../dist/llm-hub-$target-skill.zip"
  (cd "$target" && zip -qr "../../dist/llm-hub-$target-skill.zip" llm-hub)
  echo "built dist/llm-hub-$target-skill.zip"
done
mkdir -p ~/.claude/skills/llm-hub
cp claude/llm-hub/SKILL.md ~/.claude/skills/llm-hub/SKILL.md
echo "installed ~/.claude/skills/llm-hub/SKILL.md"
