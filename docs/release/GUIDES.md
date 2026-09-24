# Guides by role

Short guides for the trial. The full step-by-step walkthrough is the human review package kept with
the project records.

## Assistant (reception)
- Sign in on the staff app. You see patients, appointments, uploads, stock, reminders and call-backs.
- Book, confirm portal requests, reschedule and cancel on **Appointments**; the calendar refuses a
  time outside the clinic's hours or the dentist's roster.
- Record desk payments on the invoice; you cannot change an invoice's lines.
- Upload notes and documents; uploaded ones wait for a dentist before they join the record.
- The clinical history, next-visit summaries, document review and similar cases need the dentist's
  clinical access (`read_clinical`); reception does not hold it.

## Dentist
- Everything clinical: notes (typed or reviewing an upload), next-visit summary drafts (you approve
  or reject; nothing is approved for you), documents review, billing changes, data requests.
- Similar cases only when the clinic has switched them on; they are past records, never advice.
- Record edits go through a confirm screen showing the change before it is saved.

## Admin
- Staff accounts (create, change role, unlock) and duplicate-patient review. No clinical access.

## Patient
- Sign in to the portal with your codice fiscale and the PIN the clinic gives you.
- See and cancel your appointments, ask for a new one (a morning or afternoon on a day), see what you
  owe and what you paid, manage your consent, ask for a copy or deletion of your data.
- The assistant answers only about your own records and the clinic; clinical questions go to a person.
