#!/bin/bash
# Career-Ops Streamlit launcher

echo "🎯 Career-Ops"
echo "============="
echo ""

# Optional: warn if Ollama not running (not required — Gemini/Claude work without it)
if curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "✅ Ollama is running (local models available)"
else
    echo "⚠️  Ollama not running — local models unavailable (Gemini/Claude still work)"
fi

# Optional: warn if .env missing
if [ ! -f .env ]; then
    echo "⚠️  .env not found — API keys not loaded (add ANTHROPIC_API_KEY / GEMINI_API_KEY)"
else
    echo "✅ .env found"
fi

echo ""
echo "🚀 Starting Streamlit..."
echo "   → http://localhost:8501"
echo ""

# Use the full path to streamlit to avoid PATH issues
STREAMLIT_BIN="/Users/yingkaichen/Library/Python/3.9/bin/streamlit"

if [ -f "$STREAMLIT_BIN" ]; then
    "$STREAMLIT_BIN" run app.py
else
    python3 -m streamlit run app.py
fi
