#!/usr/bin/env node
/**
 * ollama-eval.mjs — Ollama-powered Job Offer Evaluator for career-ops
 *
 * A local, free alternative using Ollama + Mistral.
 * Reads evaluation logic from modes/oferta.md + modes/_shared.md,
 * reads the user's resume from cv.md, and evaluates a Job Description
 * passed as a command-line argument.
 *
 * Usage:
 *   node ollama-eval.mjs "Paste full JD text here"
 *   node ollama-eval.mjs --file ./jds/my-job.txt
 *   node ollama-eval.mjs --model llama2 "Your JD here"
 *
 * Requires:
 *   Ollama running locally (ollama serve)
 *   Model pulled: ollama pull mistral (or your preferred model)
 */

import { readFileSync, existsSync, readdirSync, mkdirSync, writeFileSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------
const ROOT = dirname(fileURLToPath(import.meta.url));

const PATHS = {
  shared:   join(ROOT, 'modes', '_shared.md'),
  oferta:   join(ROOT, 'modes', 'oferta.md'),
  evaluate: join(ROOT, '.claude', 'skills', 'career-ops', 'SKILL.md'),
  cv:       join(ROOT, 'cv.md'),
  reports:  join(ROOT, 'reports'),
  tracker:  join(ROOT, 'data', 'applications.md'),
};

// ---------------------------------------------------------------------------
// CLI argument parsing
// ---------------------------------------------------------------------------
const args = process.argv.slice(2);

if (args.length === 0 || args[0] === '--help' || args[0] === '-h') {
  console.log(`
╔══════════════════════════════════════════════════════════════════╗
║        career-ops — Ollama Evaluator (Local/Free)               ║
╚══════════════════════════════════════════════════════════════════╝

  Evaluate a job offer using Ollama (runs locally, zero API costs).

  USAGE
    node ollama-eval.mjs "<JD text>"
    node ollama-eval.mjs --file ./jds/my-job.txt
    node ollama-eval.mjs --model mistral "<JD text>"

  OPTIONS
    --file <path>    Read JD from a file instead of inline text
    --model <name>   Ollama model to use (default: mistral)
    --no-save        Do not save report to reports/ directory
    --help           Show this help

  SETUP
    1. Install Ollama: https://ollama.ai
    2. Pull a model: ollama pull mistral
    3. Start Ollama: ollama serve
    4. Run: node ollama-eval.mjs "Your JD here"

  EXAMPLES
    node ollama-eval.mjs "We are looking for a Senior AI Engineer..."
    node ollama-eval.mjs --file ./jds/openai-swe.txt
    node ollama-eval.mjs --model llama2 "Your JD here"
`);
  process.exit(0);
}

// Parse flags
let jdText = '';
let modelName = process.env.OLLAMA_MODEL || 'mistral';
let saveReport = true;

for (let i = 0; i < args.length; i++) {
  if (args[i] === '--file' && args[i + 1]) {
    const filePath = args[++i];
    if (!existsSync(filePath)) {
      console.error(`❌  File not found: ${filePath}`);
      process.exit(1);
    }
    jdText = readFileSync(filePath, 'utf-8').trim();
  } else if (args[i] === '--model' && args[i + 1]) {
    modelName = args[++i];
  } else if (args[i] === '--no-save') {
    saveReport = false;
  } else if (!args[i].startsWith('--')) {
    jdText += (jdText ? '\n' : '') + args[i];
  }
}

if (!jdText) {
  console.error('❌  No Job Description provided. Run with --help for usage.');
  process.exit(1);
}

// ---------------------------------------------------------------------------
// File helpers
// ---------------------------------------------------------------------------
function readFile(path, label) {
  if (!existsSync(path)) {
    console.warn(`⚠️   ${label} not found at: ${path}`);
    return `[${label} not found — skipping]`;
  }
  return readFileSync(path, 'utf-8').trim();
}

function nextReportNumber() {
  if (!existsSync(PATHS.reports)) return '001';
  const files = readdirSync(PATHS.reports)
    .filter(f => /^\d{3}-/.test(f))
    .map(f => parseInt(f.slice(0, 3)))
    .filter(n => !isNaN(n));
  if (files.length === 0) return '001';
  return String(Math.max(...files) + 1).padStart(3, '0');
}

// ---------------------------------------------------------------------------
// Load context files
// ---------------------------------------------------------------------------
console.log('\n📂  Loading context files...');

const sharedContext  = readFile(PATHS.shared,   'modes/_shared.md');
const ofertaLogic    = readFile(PATHS.oferta,   'modes/oferta.md');
const cvContent      = readFile(PATHS.cv,       'cv.md');

// ---------------------------------------------------------------------------
// Build the system prompt (mirrors the Claude skill router logic)
// ---------------------------------------------------------------------------
const systemPrompt = `You are career-ops, an AI-powered job search assistant.
You evaluate job offers against the user's CV using a structured A-G scoring system.

Your evaluation methodology is defined below. Follow it exactly.

═══════════════════════════════════════════════════════
SYSTEM CONTEXT (_shared.md)
═══════════════════════════════════════════════════════
${sharedContext}

═══════════════════════════════════════════════════════
EVALUATION MODE (oferta.md)
═══════════════════════════════════════════════════════
${ofertaLogic}

═══════════════════════════════════════════════════════
CANDIDATE RESUME (cv.md)
═══════════════════════════════════════════════════════
${cvContent}

═══════════════════════════════════════════════════════
IMPORTANT OPERATING RULES FOR THIS CLI SESSION
═══════════════════════════════════════════════════════
1. You do NOT have access to WebSearch, Playwright, or file writing tools.
   - For Block D (Comp research): provide salary estimates based on your training data, clearly noted as estimates.
   - For Block G (Legitimacy): analyze the JD text only; skip URL/page freshness checks.
   - Post-evaluation file saving is handled by the script, not by you.
2. IMPORTANT: Generate Blocks A through G in full, ALWAYS in English. Do not use any other language.
   - Even if the JD is in another language, respond in English.
   - Translate as needed, but output must be in English.
3. At the very end, output a machine-readable summary block in this exact format:

---SCORE_SUMMARY---
COMPANY: <company name or "Unknown">
ROLE: <role title>
SCORE: <global score as decimal, e.g. 3.8>
ARCHETYPE: <detected archetype>
LEGITIMACY: <High Confidence | Proceed with Caution | Suspicious>
---END_SUMMARY---
`;

// ---------------------------------------------------------------------------
// Call Ollama API
// ---------------------------------------------------------------------------
console.log(`🤖  Calling Ollama (${modelName})... this may take a minute.\n`);

async function callOllama(model, systemMsg, userMsg) {
  try {
    const response = await fetch('http://localhost:11434/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: model,
        prompt: `${systemMsg}\n\nJOB DESCRIPTION TO EVALUATE:\n\n${userMsg}`,
        stream: false,
        temperature: 0.4,
      }),
    });

    if (!response.ok) {
      const error = await response.text();
      throw new Error(`Ollama error (${response.status}): ${error}`);
    }

    const data = await response.json();
    return data.response;
  } catch (err) {
    console.error('❌  Ollama API error:', err.message);
    if (err.message.includes('ECONNREFUSED') || err.message.includes('localhost')) {
      console.error('    Make sure Ollama is running: ollama serve');
    }
    process.exit(1);
  }
}

const evaluationText = await callOllama(modelName, systemPrompt, jdText);

// ---------------------------------------------------------------------------
// Display evaluation
// ---------------------------------------------------------------------------
console.log('\n' + '═'.repeat(66));
console.log('  CAREER-OPS EVALUATION — powered by Ollama (' + modelName + ')');
console.log('═'.repeat(66) + '\n');
console.log(evaluationText);
console.log('\n' + '═'.repeat(66));

// ---------------------------------------------------------------------------
// Save report (optional)
// ---------------------------------------------------------------------------
if (saveReport) {
  try {
    const reportNum = nextReportNumber();
    const timestamp = new Date().toISOString().split('T')[0];
    const fileName = `${reportNum}-evaluation-${timestamp}.md`;
    const reportPath = join(PATHS.reports, fileName);

    if (!existsSync(PATHS.reports)) {
      mkdirSync(PATHS.reports, { recursive: true });
    }

    const reportContent = `# Career-Ops Evaluation Report
Generated: ${new Date().toISOString()}
Model: Ollama (${modelName})

## Job Description Input
\`\`\`
${jdText}
\`\`\`

## Evaluation Results
${evaluationText}
`;

    writeFileSync(reportPath, reportContent, 'utf-8');
    console.log(`✅  Report saved to: ${reportPath}`);
  } catch (err) {
    console.warn(`⚠️   Could not save report: ${err.message}`);
  }
}
