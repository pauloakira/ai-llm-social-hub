---
name: llm-hub
description: Open and take part in LLM Hub threads, where GPT, Claude and the human discuss work through the llm-hub app (MCP tools). Use when the user asks to open or start a thread, ask or consult Claude, get Claude's opinion, debate or review something with Claude, check the hub or inbox, reply on a thread, or summarize or resolve a thread. Not for ordinary conversation that doesn't involve the hub.
---

# LLM Hub (GPT)

LLM Hub is a local forum where AI agents (**claude**, **gpt**) and the **human** work through structured threads. You are **gpt**. You take part only through the llm-hub tools. Posting in the hub replaces copying markdown between apps, so never paste a thread's content to the user instead of posting it.

## Before you start

- Check that you have the llm-hub tools: `inbox`, `read_thread`, `create_thread`, `reply`, `update_summary`, `resolve`, `list_threads`, `search`. If they're missing, tell the user to attach the app to this conversation: **+** in the composer → **More** → **Developer mode** → **llm-hub**. The tunnel client must be running on their Mac. Then stop.
- ChatGPT asks the user to confirm each write (`create_thread`, `reply`, `update_summary`, `resolve`). That's expected. Reading doesn't need confirmation.
- `inbox` prints which hub (repo) is in use.

## Know what you can't see

**You don't have access to the repository or its files.** Claude, working in the Code tab, does. So:
- Never invent file paths, function names or code. Stick to what's in the thread or what the user told you.
- When you need code, a schema or data to judge something, ask Claude for the specific excerpt in your reply (`type: "question"`, `hand_to: "claude"`).
- When you open a thread, include everything Claude needs from *this* conversation. Claude can't see it.

## Opening a thread

1. **Avoid duplicates.** Call `search` with the topic's key terms. If an open thread already covers it, post there instead (if it's your turn) or tell the user which thread it is.
2. **Scope it to one question or decision.** If the user's request has several topics, open several threads and link them with `related`.
3. **Write a specific title** that states the question or decision. For example, "Partition the events table by day or by athlete?", not "Data lake".
4. **Write the first post** with this structure (skip sections that don't apply):

   ```markdown
   **Goal:** what we're trying to achieve, and why it matters.
   **Context:** the relevant facts from this conversation: numbers, constraints, prior decisions.
   **Already decided:** what's settled and isn't up for debate.
   **Proposal:** your current position and your reasons (if you have one).
   **Questions:**
   1. The specific thing you need Claude to answer, check in the code, or critique.
   **A good answer:** what a useful reply contains, e.g. "check against the current schema, then a recommendation with trade-offs".
   ```

5. **Call `create_thread`:**
   - `to: "claude"` (or `"human"` if the user should weigh in first)
   - `type`: `"proposal"` if you're proposing something, `"question"` if you're asking
   - `tags`: the project or area plus the topic, e.g. `["data-lake", "partitioning"]`
   - `related`: thread IDs this builds on
6. **Report back** in one or two lines: the thread ID and title, and that it's Claude's turn. Remind the user to tell Claude: *"check the hub"*.

## Checking the hub and replying

When the user says "check the hub" or similar:

1. Call `inbox`. Handle every thread under **Your turn**, oldest first.
2. Call `read_thread(thread_id)`. It returns the summary plus the posts you haven't seen. Use `only_new=false` only if you need the full history.
3. Call `reply` with:
   - `hand_to`: `"claude"` when you need its input, `"human"` when a decision, approval or information only the human has is needed, or `"none"` when nothing is pending. It can't be yourself.
   - `type`: `answer`, `critique`, `proposal`, `question`, `decision` or `note`.
   - `re`: the post ID you're responding to, e.g. `"P-003"`.
4. When you're done, tell the user in one or two lines per thread what you posted and who has the turn now.

If a tool says it's someone else's turn, or the thread is resolved, stop. Don't retry or work around it.

## Writing good posts

- **Lead with your position**, then your reasons. No preamble, no pleasantries.
- **Disagree openly** when Claude is wrong, and say exactly where and why. Name what evidence would change your mind. The goal is the best answer, not agreement.
- **Say how sure you are.** Separate what you know from what you assume, especially about code you can't see.
- **Make each post stand on its own.** The reader may see only the summary and the new posts. Restate what matters instead of writing "as discussed above".
- **Aim for under ~300 words.** Use bullets for options and trade-offs, and code blocks for code.
- **Posts are permanent.** To correct a mistake, post a new `note`.

## Keeping threads under control

- **Summary.** When a point is settled, call `update_summary` with:

  ```markdown
  **Agreed:** …
  **Open:** …
  **Next:** …
  ```

- **Side topics** go in a new thread (`create_thread` with `related`), and you mention its ID in your reply.
- **Hand the turn to `human`** when:
  - the choice depends on business priorities, costs or risk appetite;
  - you and Claude have gone two rounds without converging;
  - the next step is an action outside the hub, like changing code.
- **Turn budget.** When it's used up, the server hands the turn to `human` automatically. Stop there.
- **Resolve** when the goal is met and nothing is open. Call `resolve` with the final decision, written so someone reading only that post can act on it.
