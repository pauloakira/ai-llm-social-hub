#!/usr/bin/env bash
# Build uploadable skill zips in dist/ and install the skills locally for Claude Code and ChatGPT desktop/Codex.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p ../dist
for target in claude gpt; do
  rm -f "../dist/llm-hub-$target-skill.zip"
  (cd "$target" && zip -qr "../../dist/llm-hub-$target-skill.zip" llm-hub)
  echo "built dist/llm-hub-$target-skill.zip"
done
install_skill() {  # <source dir> <skills root>
  rm -rf "$2/llm-hub" && mkdir -p "$2" && cp -R "$1" "$2/llm-hub"
  echo "installed $2/llm-hub"
}
install_skill claude/llm-hub ~/.claude/skills   # Claude Code (Code tab)
install_skill gpt/llm-hub ~/.codex/skills       # ChatGPT desktop / Codex
