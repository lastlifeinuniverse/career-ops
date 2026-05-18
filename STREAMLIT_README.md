# Career-Ops Streamlit MVP

A unified web UI for your job search pipeline. Combines Claude (scanning) + Ollama (evaluation) in one beautiful dashboard.

## Features

✅ **🔍 Scan & Browse** — Use Claude to find jobs matching your criteria
✅ **📋 Evaluate Jobs** — Evaluate JDs locally with Ollama (zero cost)
✅ **⚖️ Compare Jobs** — Side-by-side comparison of multiple opportunities
✅ **📊 Dashboard** — Track applications, see top matches, monitor progress

## Quick Start

### 1. Install Dependencies

```bash
cd career-ops
pip install -r requirements-streamlit.txt
```

### 2. Set Up Environment

Make sure you have:
- ✅ `.env` file with `ANTHROPIC_API_KEY` (for Claude scanning)
- ✅ `Ollama` running locally (`ollama serve`)
- ✅ `mistral` model downloaded (`ollama pull mistral`)
- ✅ `cv.md` and `modes/` directory (from career-ops setup)

### 3. Run the App

```bash
streamlit run app.py
```

The app will open at `http://localhost:8501`

---

## Architecture

### Pages

#### 🔍 Scan & Browse
- Enter search criteria (keywords, locations, min salary)
- Claude scans job boards and returns matches
- Results saved to SQLite database
- Browse all discovered jobs

#### 📋 Evaluate Job
- **Paste JD**: Manually paste a job description
- **Evaluate Discovered**: Select an unevaluated discovered job
- Ollama runs evaluation using your CV + evaluation logic
- Results saved with score, legitimacy, archetype
- No API costs (runs locally)

#### ⚖️ Compare Jobs
- Select 2-4 evaluated jobs
- Side-by-side comparison table
- Detailed evaluation breakdown for each
- Easy decision-making

#### 📊 Dashboard
- Summary stats (total jobs, evaluated, applied, avg score)
- Top 10 opportunities ranked by score
- Full job list with status tracking
- Update application status and add notes

### Database Schema

```
jobs
├── company, title, description
├── salary, location
├── status (discovered, evaluated, applied, etc.)
└── source (claude_scan, manual_paste)

evaluations
├── job_id (foreign key)
├── score, blocks (full A-G evaluation)
├── legitimacy, archetype
└── model (mistral, claude, etc.)

applications
├── job_id (foreign key)
├── status (interested, applied, rejected, offer)
├── applied_date, notes
└── timestamps
```

### API Integration

**Claude (Scanning)**
- Model: `claude-opus-4-7`
- Purpose: Find 5-10 jobs matching your criteria
- Cost: ~1-3K tokens per scan
- Speed: ~10-20 seconds

**Ollama (Evaluation)**
- Model: `mistral` (local)
- Purpose: Evaluate job with A-G scoring
- Cost: $0 (runs on your machine)
- Speed: ~30-60 seconds per job

---

## Cost Breakdown

Weekly usage scenario:
- 1x scan with Claude: ~2-3K tokens (~$0.03)
- 10x evaluations with Ollama: $0
- **Total/week: ~$0.03**
- **Total/year: ~$1.50**

---

## Troubleshooting

### "Cannot connect to Ollama"
```bash
# Start Ollama in a separate terminal
ollama serve

# Make sure mistral is downloaded
ollama pull mistral
```

### "ANTHROPIC_API_KEY not found"
```bash
# Add to your .env file
echo "ANTHROPIC_API_KEY=your_key_here" >> .env
```

### "modes/_shared.md not found"
Make sure you're running from the career-ops root directory and have the evaluation modes set up.

---

## Workflow Tips

**Optimal workflow:**
1. Scan jobs once per week with Claude (~$0.03)
2. Evaluate discovered jobs with Ollama ($0 each)
3. Compare top matches side-by-side
4. Update application status in dashboard
5. Check progress in summary stats

**Cost savings vs alternatives:**
- Claude for everything: ~$31/year
- This (Claude + Ollama): ~$1.50/year
- **You save ~95%!**

---

## Future Enhancements

- [ ] Email integration (get notified of new matches)
- [ ] Automated periodic scanning
- [ ] Resume tailoring suggestions
- [ ] Interview prep recommendations
- [ ] Export reports (PDF, CSV)
- [ ] Dark mode theme
- [ ] Mobile responsive design

---

## Files

- `app.py` — Main Streamlit application
- `db_init.py` — SQLite database setup
- `api_integration.py` — Claude + Ollama integrations
- `requirements-streamlit.txt` — Python dependencies
- `career_ops.db` — SQLite database (created on first run)

---

## Support

Having issues? Check:
1. Is Ollama running? (`ollama serve`)
2. Is ANTHROPIC_API_KEY set in .env?
3. Do you have cv.md and modes/ directory?
4. Are you in the career-ops root directory?
