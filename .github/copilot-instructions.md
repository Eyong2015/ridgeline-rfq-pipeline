# Conversation history

At the start of each session, read `.chat-history/log.md` for prior context. If
the file or its parent directory does not exist, create it.

After every assistant response, silently append one entry to
`.chat-history/log.md` using this format:

---
- timestamp: "<ISO 8601 timestamp if available, otherwise estimate based on conversation order>"
- user_prompt: "<the user's original prompt>"
- assistant_response_summary: "<a concise, specific summary of what you generated or answered>"
- files_affected: "<comma-separated list of files created or modified during this response, or none>"

Create the parent directory and file as needed. Append only; never overwrite or
delete previous entries. Include only files explicitly created or modified for
that response in `files_affected`. Keep summaries concise and specific,
including relevant function names, endpoints, or key decisions.

Before writing an entry, redact secrets in every field, including the user
prompt. Replace API keys, tokens, passwords, and `.env` values with `***`.

Do this silently and do not ask for confirmation.
