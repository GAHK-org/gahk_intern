# Optagelse

The public admissions pages, where prospective residents request a rundvisning or apply for fremleje, and Indstillingen's list for reviewing what comes in.

## Public pages
- Anyone can use the admissions pages; no login is needed.
- The landing page (`/optagelse/`) explains the process (check the criteria, request a tour, apply after the tour), shows the admission criteria and links to both forms.
- **Rundvisning** is for people who want to live here permanently; a tour is the first step. The form asks for name, email, gender, age, years studied, study years left, university, field of study, where they heard about us and an optional motivation of at most 500 characters. The admission criteria are shown alongside it.
- **Fremleje** is for living here temporarily via a current resident. The form asks for name, email, gender, age, occupation, where they heard about us and an optional motivation.
- Name, email, gender and "where did you hear about us" (a fixed list of choices) are required on both forms.
- Both forms are protected by a captcha and a hidden spam trap. A failed captcha shows an error and nothing is saved. The captcha is on only when both of its keys are configured.
- After a successful submission the applicant sees a thank-you page and gets a "we have received your request" email.
- Indstillingen is not emailed about new applications; they work from the list.

## Review (Indstillingen)
- Only Indstillingen (and administrators) can see the applications. The sidebar shows "Ansøgninger" to them.
- The list shows both types together, 50 per page, newest first, with each application's receiver or "Afventer".
- The list can be:
  - searched across name, email, education, occupation, "heard about us" and motivation;
  - sorted by date, type, name, education or receiver, in either direction;
  - limited to applications not yet received;
  - limited to a submission date range.
- The detail page shows every answer, who marked it received and when, and whether it is discarded.
- **Mark received** records who did it and when. The first person to mark it wins, and it cannot be undone.
- **Discard** hides spam or obvious non-fits from the list and search. It is a toggle: a discarded application can be restored, and a "show discarded" switch reveals them. The list shows how many are discarded.
- **Export** downloads the current list as CSV or Excel. It uses the same search, filters, sort and date range as the list, and contains contact details only: date, type, name, email and education/occupation. Cells that would start a spreadsheet formula are neutralised.

## Retention
- Applications older than one year are deleted every night, whether received, discarded or neither.
- Approved administrators can also see and edit applications in Django admin.

## Decisions
- No email goes to the committee for each application. Indstillingen asked to stop the per-request mail and review the list instead.
- If the confirmation email fails, the error is logged rather than ignored, but the application is still saved. A misconfigured sender address must not fail silently.
- Discard is a reversible toggle rather than a delete, so a mistaken discard can be undone. The one-year deletion still applies.
- Half a captcha configuration counts as off, with a warning at startup: with only the secret key no captcha is shown, so every application would be refused.
- Formulas are neutralised in the export because every column is typed by an anonymous applicant.
- Marking an application received requires a form submit, not a plain link. The old link could be triggered by a forged request.
