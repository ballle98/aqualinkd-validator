# Local Repository Instructions

## Commit attribution

When AI tools contribute to development, proper attribution helps track the evolving role of AI in the development process. Contributions should include an Assisted-by tag in the following format:

```text
Assisted-by: LLM [TOOL1] [TOOL2]
```

[TOOL1] [TOOL2] are optional specialized analysis tools used (e.g., coccinelle, sparse, smatch, clang-tidy). Basic development tools (git, gcc, make, editors) should not be listed.

Example:

```text
Assisted-by: LLM coccinelle sparse
```

See https://docs.kernel.org/process/coding-assistants.html

Keep issue references fully qualified, for example
`Refs ballle98/aqualinkd-validator#8`.
