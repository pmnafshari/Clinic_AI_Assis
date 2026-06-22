# Dental AI Assistant

A private, offline assistant for a dental clinic. It reads messy dentist notes and turns
them into clean structured data, sorts incoming files, stores everything, and answers
questions and edits records. Built with small local models and free tools.

Built one phase at a time. Development uses fake data only.

## Setup (Mac)

1. Install Ollama from https://ollama.com/download
2. Pull and test the local model:
   ```
   ollama pull llama3.2
   ollama run llama3.2 "say hello in one short sentence"
   ```
3. Create the virtual environment and install the libraries:
   ```
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

## Setup (Colab GPU)

Training runs on the free Colab GPU.

1. Open `notebooks/colab_gpu_check.ipynb` in https://colab.research.google.com
2. Runtime > Change runtime type > T4 GPU
3. Run all cells and confirm the GPU is detected

Note: torch is pre-installed on the Colab GPU runtime, so it is intentionally not in
`requirements.txt`. The Mac does not need torch.

## Phases

See `Dental_AI_Roadmap.md` for the full plan. Phase 1 sets up the environment, the
shorthand glossary, and the notes schema.
