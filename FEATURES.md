# Dental AI Assistant: features and what they are for

## Staff app (English and Italian, port 5000)

- **Patients list and record**: find a patient; see details, visit timeline, invoices and consents.
- **New patient**: register a walk-in with a required phone and the document the code was checked against.
- **Duplicates review**: spot possible duplicate records; merge them or mark them as different people.
- **Appointments**: day, week and month calendar; book, move or cancel; confirm or decline patient requests.
- **Requests**: appointment, imaging and data requests in one place.
- **Add note**: turn a messy dentist note into a draft to check before saving; treatments the note doesn't say are taken out.
- **Notes to review**: check uploaded and earlier notes, then confirm or reject them.
- **Documents and files**: upload patient files, preview them, confirm whose they are; a file whose original is missing can be recorded as lost, with a reason.
- **Legacy import**: bring in files from the old shared folder (or a Drive test folder) for a dentist to confirm one by one.
- **Next-visit summary**: draft a summary from the notes; flags conflicts (such as a note date that differs from the visit date); needs a dentist's approval.
- **Dental chart (odontogram)**: FDI numbering, adult and baby teeth; each kind of work has its own colour, code and shape, planned or done; work the notes name is suggested in a popup and drawn only after the dentist approves it; dentist only.
- **Care plan**: the services a patient is referred to (hygiene, restorative, endodontics, surgery, implants, prosthetics, orthodontics, paediatric, radiology); dentist and reception add them and mark them done.
- **Aftercare**: what to do after each treatment, in Italian and English; demo sample texts until a dentist writes and approves the clinic's own; the patient sees only approved ones.
- **Similar cases**: compare with anonymised past cases from other patients; switched off until the criteria are approved.
- **Ask records**: ask a plain question ("When is Mario Rossi's next appointment?") and get a sourced answer.
- **Run a command**: change a record by command, with a confirm step.
- **Billing**: invoices, recorded payments, refunds, installments; no real money moves through it.
- **Stock**: supplies and low-stock alerts.
- **Reminders**: queue of appointment and invoice reminders; sending is off until a provider is chosen.
- **Call-backs**: patients who asked for a person.
- **Tasks**: give a task to a dentist or reception with a due time, optionally about a patient; overdue ones show on Home, in the bell and the menu; nothing is sent.
- **Win back**: patients not seen for 12 months or more, with nothing booked, who agreed to offers and news, for reception to call by hand; nothing is sent.
- **Data requests**: access, copy, correction and erasure requests from patients.
- **Reports**: clinic charts, the dentist's own work, CSV exports.
- **Ask clinic guides**: questions answered from approved manuals and procedures, quoted from the page.
- **Imaging requests**: a dentist orders an X-ray; reception books it, marks it done and links the file.
- **Admin pages**: staff accounts and roles, outside connections (messaging, payments, telephony: all off), Nana devices.
- **Language switch**: English or Italian on every page.
- **Outage guard**: if the server stops, a save shows "not answering" and keeps what you typed.

## Patient portal (port 5001)

- **PIN sign-in**: patients see only their own data.
- **Visits, invoices, files, profile**: read their own history.
- **Appointment requests**: ask for a day and a period; cancel one.
- **Assistant chat**: ask about visits, next appointment, invoices or details.
- **Data requests**: ask for a copy of their data and tick which parts.
- **Consent**: allow or refuse the assistant, and offers and news.
- **Aftercare**: the approved aftercare text for a recent treatment, on the home page and in the chat.

## Public site (port 5002)

- **Clinic website**: services, doctors, hours, contact.
- **Voice assistant**: general clinic questions; off unless the server runs with `VOICE_DEMO=1`.

## Behind the scenes

- **Local notes model (Ollama)**: reads notes and files on this machine only; nothing goes to the cloud.
- **Intake pipeline**: upload, extract, sort and store notes.
- **Audit log**: records who did what.
- **Roles and permissions**: dentist, reception and admin each see only what they should.
- **Backups and tested restore**: protect the data.
- **Erasure with a retention hold**: remove a patient while keeping invoices the policy holds.
- **Nana (voice companion) and Clinic Workspace app**: Nana listens on this Mac, answers from approved guides and, for a signed-in doctor, opens records, reads next appointments, adds notes and changes a phone number - always on screen first; the macOS wrapper shows the clinic pages and Nana.
- **Nana's card**: on every page a doctor opens - on, standby or off; turning it on signs Nana in as that doctor without a password; the question, then the answer with buttons to the record or page.
- **Phone line (off until a provider is chosen)**: a caller identifies with the codice fiscale and the last three digits of the phone on file; five wrong tries lock it for 30 minutes.
