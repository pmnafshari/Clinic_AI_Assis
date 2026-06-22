# Dental AI Assistant — Full Roadmap

**Owner:** Peyman
**Hardware:** MacBook M3, 16GB RAM + Google Colab (free)
**Style:** Build one phase at a time. Test each phase before the next.
**Language of notes:** English. **First dataset size:** 180 notes.

---

## The big idea (in one picture)

You are building a private assistant for a dental clinic. It will:

1. Read messy dentist **notes** and turn them into clean data. *(your own model — built first)*
2. **Sort** new files from the nurse automatically (Excel, notes, images).
3. Store everything so you can **ask questions** and **change records** by command.
4. Later: **read and analyse mouth X-rays** with your own vision model.
5. Optional, last: control it all by **voice**.

We build your own models, not a generic chatbot. We use **free** tools. While building, we use **fake data**. Real patient data only comes in at the end, on a local (offline) setup, for privacy law (GDPR).

---

## Where each thing runs

| Job | Runs on | Why |
|---|---|---|
| Writing code, testing, daily use | **Mac M3** | Light work, always with you |
| Training / fine-tuning models | **Colab (free GPU)** | Heavy work, needs a GPU |
| Running finished small models | **Mac M3** | 16GB is enough for small models |
| Real patient data (final stage) | **Mac, offline** | Privacy. Never send real data to cloud |

**Memory rule for the Mac:** never load two big models at the same time. Load one, use it, unload it.

---

## Tech stack (free)

- **Language:** Python
- **Notes model:** small instruction model (Llama 3.2 3B *or* Phi-3.5-mini) + **LoRA/QLoRA** fine-tuning
- **Training tools:** Unsloth or Hugging Face PEFT + Transformers (on Colab)
- **Run model on Mac:** Ollama or MLX (Apple Silicon friendly)
- **Data check:** Pydantic (validates the model output)
- **File watcher (auto-sort):** `watchdog` Python library
- **Structured database:** SQLite (simple) or MongoDB
- **Vector database:** ChromaDB
- **Embeddings:** `all-MiniLM-L6-v2` (small, free, local)
- **Agent / tools:** LangChain or LlamaIndex
- **X-ray model (later):** YOLO (Ultralytics) fine-tuned on free public dental datasets
- **Voice (optional):** faster-whisper (speech-to-text), Porcupine (wake word), Piper (text-to-speech)

---

## Phase 0 — Setup & foundation *(almost done)*

**Goal:** Get tools ready and agree the data shape.

**Steps**
1. Install Python tools on Mac (Ollama, Python 3.11, basic libraries).
2. Set up a free Colab account and test the GPU.
3. Build the **short-hand glossary** (done ✅ `dental_shorthand_glossary.json`).
4. Build the **notes schema** (done ✅ `dental_notes_schema.py`).

**Deliverables:** working environment, glossary, schema.
**Done when:** you can run Python on Mac and open a GPU notebook on Colab.
**Time:** ~half a day.

---

## Phase 1 — Notes Understanding Model *(FIRST — your own model)*

**Goal:** A model that reads a messy dentist note and outputs clean JSON, and understands short-hand.

**Why first:** It is the easiest custom model to get working, gives a fast win, and feeds every later phase.

**Runs where:** Train on Colab. Run on Mac.

**Data needed:** 180 fake notes (English) + the correct JSON for each. We build these together.

**Steps**
1. **Make the dataset.** Generate 180 realistic messy notes with short-hand. For each, write the correct clean JSON (matches the schema). Split: 150 train / 30 test.
2. **Pick the base model.** Start with Llama 3.2 3B Instruct (or Phi-3.5-mini). Small enough for Colab free and the Mac.
3. **Format the data** for training (instruction → input note → output JSON). Add the glossary into the instruction so the model learns short-hand.
4. **Fine-tune with LoRA/QLoRA** on Colab GPU. This is cheap and fast for a small model.
5. **Export the model** in a Mac-friendly format (GGUF for Ollama, or MLX).
6. **Run on Mac** and check it.

**Deliverables:** `notes_dataset.jsonl`, training notebook, the fine-tuned model, a small `extract_note.py` script.

**Risks & fix**
- *Too little data → weak model.* Fix: start with 180, grow to 500+ if needed.
- *Model invents data (hallucination).* Fix: Pydantic validation + tell the model "only use what is written."

**Done when:** on the 30 test notes, the model fills the JSON correctly most of the time (target: 85%+ fields correct).
**Time:** ~3–5 days.

---

## Phase 2 — Clinic Folder + Auto-Categorizer *(your goal #4)*

**Goal:** A folder that works like a real clinic computer (messy), and a watcher that **sorts new files instantly** when the nurse drops them in.

**Runs where:** Mac.

**Steps**
1. **Build a fake clinic folder.** Messy file names, nested folders. Excel = patient info + invoices. `.txt` = visit notes. Images = X-rays (fake for now).
2. **Build the watcher** with `watchdog`. It wakes up the moment a new file appears.
3. **Sort by type first** (fast rule): `.xlsx` → records, `.txt` → notes, `.jpg/.png` → images.
4. **Then sort by content** (smart): the notes model reads a new note and finds which patient it belongs to (by name / Codice Fiscale).
5. **Move/copy** the file to the right patient folder and write a log.

**Deliverables:** fake clinic folder, `watcher.py`, sorting rules, a log file.

**Risks & fix**
- *Wrong patient match.* Fix: if unsure, put it in a "needs review" folder, not the wrong place.

**Done when:** you drop a new file in, and within seconds it lands in the correct patient folder (or "needs review").
**Time:** ~2–3 days.

---

## Phase 3 — Databases & Storage

**Goal:** Store clean data so it is fast to search two ways: exact match and meaning match.

**Runs where:** Mac.

**Steps**
1. **Structured DB (SQLite or MongoDB):** exact data — name, Codice Fiscale, phone, invoices, visit dates. Good for "What is Ali's phone?"
2. **Vector DB (ChromaDB):** the descriptive text — diagnosis, history, summaries. Each chunk stores the patient's Codice Fiscale as a link.
3. **Embeddings:** turn text into vectors with `all-MiniLM-L6-v2` (local, free).
4. **Loader script:** push the JSON from Phase 1 into both databases.

**Deliverables:** database files, `load_to_db.py`, embedding script.
**Done when:** every patient's data sits in both databases and links match.
**Time:** ~2–3 days.

---

## Phase 4 — Retrieval & Q&A (RAG)

**Goal:** Ask questions in plain English and get answers from the data only (no making things up).

**Runs where:** Mac.

**Steps**
1. **Query router:** decide the question type. Exact ("phone number") → structured DB. Meaning ("who has severe gum disease?") → vector DB.
2. **Fetch** the right records.
3. **Answer** using a model, but tell it: "answer only from this data."
4. Show the **source** (which patient/file) with each answer.

**Deliverables:** `ask.py`, router logic, test questions.
**Risks & fix:** *model adds facts.* Fix: strict prompt + always show source.
**Done when:** it answers test questions correctly and points to the source.
**Time:** ~3–4 days.

---

## Phase 5 — Action Agent (change records)

**Goal:** Give commands that **edit** data, not just read it. Example: "Add a 50 euro scaling charge to Reza's file."

**Runs where:** Mac.

**Steps**
1. **Write tools** (small Python functions):
   - `update_patient_record(cf, new_data)` → updates the structured DB.
   - `append_to_notes(file, text)` → edits a `.txt` note.
   - `add_invoice_row(excel, service, amount)` → adds a row to Excel.
2. **Bind tools** to the model (LangChain/LlamaIndex).
3. **The loop:** command → pick the right tool → fill the values → run it → confirm back.
4. **Safety:** ask "are you sure?" before any change, and keep a backup/undo log.

**Deliverables:** `tools.py`, agent script, change log.
**Risks & fix:** *wrong edit.* Fix: confirm step + backups + a "dry run" mode.
**Done when:** a spoken/typed command correctly changes the right file and confirms.
**Time:** ~4–6 days.

---

## Phase 6 — X-ray Vision Model *(your goals #1 and #3)*

**Goal:** Your own model that looks at a mouth X-ray, finds teeth, and flags problems (decay, bone loss, etc.).

**Runs where:** Train on Colab GPU. Run on Mac.

**Data needed:** You said you have no X-rays yet. So we start with **free public datasets**: DENTEX (panoramic X-rays with tooth numbers + problems), Tufts Dental Database, and dental datasets on Roboflow/Kaggle.

**Steps**
1. **Get a public labeled dataset** and understand its labels.
2. **Choose the task:** start simple — detect and number teeth. Then add problem detection (caries, lesions).
3. **Fine-tune YOLO** (Ultralytics) on Colab GPU.
4. **Test** on held-out images. Measure accuracy.
5. **Run on Mac** for daily use.
6. Later: collect the clinic's own X-rays and a dentist labels a few to improve the model.

**Deliverables:** training notebook, the X-ray model, `analyse_xray.py`.
**Risks & fix**
- *Public data looks different from clinic X-rays.* Fix: fine-tune again later on real clinic images.
- **Medical safety:** this is a **helper for the dentist, not a replacement**. The dentist always checks. Accuracy is never 100%.

**Done when:** on test X-rays, the model marks teeth and obvious problems at a useful accuracy.
**Time:** ~1–2 weeks (this is the hardest phase).

---

## Phase 7 — Multimodal Linking

**Goal:** Connect the X-ray results to the patient record and the rest of the system.

**Steps**
1. When an X-ray is analysed, save the result (teeth, findings) with the patient's Codice Fiscale.
2. Store it in the databases so Q&A can use it ("show patients with bone loss on lower molars").
3. Show the marked X-ray image next to the patient's notes.

**Deliverables:** link script, combined patient view.
**Done when:** an X-ray finding appears inside the right patient's record and is searchable.
**Time:** ~3–4 days.

---

## Phase 8 — Voice Interface *(optional, last)*

**Goal:** Hands-free control. Say "Hey Assistant", speak a command, hear the answer.

**Runs where:** Mac, all offline.

**Steps**
1. **Wake word** with Porcupine (listens for "Hey Assistant").
2. **Record** the voice after the wake word.
3. **Speech-to-text** with faster-whisper.
4. Send the text to the **Phase 5 agent**.
5. **Text-to-speech** with Piper to speak the answer.

**Deliverables:** `voice.py`, wake-word config.
**Risks & fix:** *noisy clinic → wrong words.* Fix: confirm important commands before acting.
**Done when:** you can do a full task by voice, hands-free.
**Time:** ~4–6 days.

---

## Phase 9 — Privacy & Switch to Real Data

**Goal:** Make it safe and legal for real patients (GDPR).

**Steps**
1. **Go fully local.** Replace any cloud model calls with a local model (Ollama). No data leaves the Mac.
2. **Encrypt** the patient folder and databases.
3. **Access control** (password / user accounts).
4. **Backups** that are also encrypted.
5. **Audit log:** who changed what and when.

**Deliverables:** offline config, encryption setup, backup plan.
**Done when:** the full system runs offline with real data, encrypted, with logs.
**Time:** ~3–5 days.

---

## Phase 10 — Daily Use & Maintenance

**Goal:** Keep it working and improving.

**Steps**
1. Collect cases where the model was wrong → add them to training data → retrain sometimes.
2. Grow the short-hand glossary as new abbreviations appear.
3. Keep backups and check logs.

**Done when:** it runs every day and slowly gets better.
**Time:** ongoing.

---

## Rough total time

Solo, learning as you go: about **6–10 weeks** to reach a strong text system (Phases 0–5), plus **2–3 weeks** for X-ray (Phase 6–7). Voice and polish add more. The notes model (Phase 1) gives a working result in the first week or two.

---

## Data needs — summary

| Phase | Data you need | Have it? |
|---|---|---|
| 1 Notes model | 180 fake English notes + JSON | We build it |
| 2 Auto-sort | Fake clinic folder | We build it |
| 6 X-ray model | Labeled X-rays | Use **free public datasets** first |
| 9 Real use | Real patient data | Only at the end, offline |

---

## Risks (top 3) and how we handle them

1. **16GB memory limit** → use small models, never load two big ones together, train heavy stuff on Colab.
2. **Model makes up facts** → Pydantic checks, strict "use only given data" prompts, always show the source.
3. **Privacy / GDPR** → build with fake data, switch to offline + encryption before any real patient data.

---

## Open questions (please answer when you can)

1. **Model run tool on Mac:** do you prefer **Ollama** (easiest) or **MLX** (Apple's own, can be faster)? *Suggestion: start with Ollama.*
2. **Structured database:** **SQLite** (simplest, one file) or **MongoDB** (matches the original plan)? *Suggestion: SQLite to start, switch later if needed.*
3. **Colab:** free version only, or can you use **Colab Pro** later? Free is enough for Phase 1; Pro helps a lot for Phase 6 (X-ray).
4. **Wake word name** for voice later: what should the assistant be called? (e.g. "Hey Denti").
5. **Real X-ray type:** will the clinic mostly use **panoramic** (OPG) or small **periapical/bitewing** X-rays? This changes which public dataset we pick.

---

## My suggestions

- **Keep Phase 1 small and fast.** Get a working notes model in week one, even if not perfect. A small win keeps momentum.
- **Use Gemini or another free API only as a "teacher"** to help generate the 180 training examples faster — but the final clinic model stays your own and local.
- **Do not skip the fake-data stage.** It lets us build freely without privacy risk.
- **Treat the X-ray model as decision-support, never a diagnosis by itself.** A dentist must always confirm.
- **Save every wrong answer.** These become your best future training data.
- **Consider Phase 2 (auto-sort) right after Phase 1.** It is quick, very visible, and the nurse will feel the value immediately.
