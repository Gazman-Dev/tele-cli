# AGENTS.md instructions for {{topic_name}}

## Workspace role

- This directory maps to one Telegram topic workstream.
- Treat this workspace as isolated from sibling topics.
- Put durable topic guidance in this file.

## Git model

- This directory is its own Git repository.
- The parent `../..` workspace may track it as a submodule-style entry.

## Topic details

- Visible topic name: `{{topic_name}}`
- Telegram chat id: `{{transport_chat_id}}`
- Telegram topic id: `{{transport_topic_id}}`
- If a dependency, tool, or script is missing, ask the operator before installing or scaffolding it globally.
