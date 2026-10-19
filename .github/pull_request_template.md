<!-- Keep the title short and imperative, e.g. "Restore Google Calendar embed on dashboard". -->

## Summary

<!-- What does this PR change and why? 1–3 sentences. Link any specs in spec/features/ or issues. -->

-

## Type of change

<!-- Delete the ones that don't apply. -->

- Feature
- Bugfix
- Refactor / cleanup
- Tests / tooling
- Docs
- Infra / deploy

## How was this tested?

<!-- Describe how you verified the change. Note which DB backend you ran against. -->

- [ ] `task test:pg` (Postgres, CI parity), requires `task db:up`
- [ ] `task test:sqlite` (no Docker)
- [ ] `task lint`
- [ ] Manually exercised the affected flow (describe below)

<!-- Manual verification notes: -->

## Checklist

- [ ] Migrations added if models changed (`task makemigrations`) and they apply cleanly (`task migrate`)
- [ ] No secrets committed — new config comes from the environment (`.env` / vault), not source
- [ ] Docs updated if behaviour/config/deploy changed (README / DEPLOY.md / spec)
- [ ] User-facing text is in Danish where appropriate

## Screenshots / notes

<!-- Optional: UI screenshots, follow-ups, or anything a reviewer should know. -->
