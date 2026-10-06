# Writing templates

Each `*.md` file here guides one family of written questions. The question catalog
(`src/applypilot/data/question_catalog.yaml`) maps questions to templates:

| template | used for |
| --- | --- |
| `why_company.md` | "Why do you want to work at X?", "What excites you about us?", "Why <Company>?" |
| `why_role.md` | "Why this role/team/internship?" |
| `technical_project.md` | "Describe a project you're proud of" |
| `challenging_project.md` | "Hardest technical problem / challenge you overcame" |
| `leadership.md` | leadership questions |
| `teamwork.md` | teamwork / collaboration |
| `conflict.md` | conflict / disagreement |
| `failure.md` | failure / mistake / setback |
| `other_common_questions.md` | "Tell us about yourself", "Anything else?" |
| `cover_letter.md` | required cover letters |

How Astra uses them: the template is **guidance** (structure, tone, which real stories to
prefer, things to avoid) — never pasted verbatim. Astra combines it with the job
description, cached company research and `profile/profile.md`, and respects word/character
limits. Answers are rejected automatically if they exceed limits, contain placeholders, or
mention institutions/GPAs that are not in your profile.

Tips: reference stories by the headings you use in `profile/profile.md`; write one short
sample answer in your own voice; list phrases you never want used.

Private versions: `why_company.local.md` overrides `why_company.md` and is git-ignored.
Add a new template by creating `<name>.md` and pointing a catalog entry's `written:` at it
(see `config/examples/question_catalog.example.yaml`).
