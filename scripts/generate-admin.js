#!/usr/bin/env node
/**
 * generate-admin.js
 *
 * Reads admin.template.html, replaces __PLACEHOLDER__ tokens with
 * environment variables, and writes the result to public/admin.html.
 *
 * Required env vars (set in Vercel dashboard or a local .env.admin file):
 *   ADMIN_SUPABASE_URL        Supabase project URL
 *   ADMIN_SUPABASE_ANON       Supabase publishable anon key
 *   ADMIN_EMAIL               Google account email allowed admin access
 *   ADMIN_GH_REPO             GitHub repo, e.g. "thekenyeung/clawbeat-v1"
 *   ADMIN_GH_BRANCH           Branch to sync, e.g. "main"
 *   ADMIN_GH_WHITELIST_PATH   Path to whitelist.json, e.g. "src/whitelist.json"
 *
 * Usage:
 *   node scripts/generate-admin.js
 *   (or via npm run generate:admin, or as part of npm run build)
 *
 * Local dev — create a .env.admin file at the project root (gitignored):
 *   ADMIN_SUPABASE_URL=https://xxxx.supabase.co
 *   ADMIN_SUPABASE_ANON=sb_publishable_...
 *   ADMIN_EMAIL=you@gmail.com
 *   ADMIN_GH_REPO=youruser/yourrepo
 *   ADMIN_GH_BRANCH=main
 *   ADMIN_GH_WHITELIST_PATH=src/whitelist.json
 */

import { readFileSync, writeFileSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, '..');

// ── Load .env.admin for local development ────────────────────────────────────
const envFile = resolve(ROOT, '.env.admin');
if (existsSync(envFile)) {
  const lines = readFileSync(envFile, 'utf8').split('\n');
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eq = trimmed.indexOf('=');
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    const val = trimmed.slice(eq + 1).trim().replace(/^["']|["']$/g, '');
    if (!(key in process.env)) process.env[key] = val;
  }
}

// ── Validate required vars ────────────────────────────────────────────────────
const REQUIRED = [
  'ADMIN_SUPABASE_URL',
  'ADMIN_SUPABASE_ANON',
  'ADMIN_EMAIL',
  'ADMIN_GH_REPO',
  'ADMIN_GH_BRANCH',
  'ADMIN_GH_WHITELIST_PATH',
];

const missing = REQUIRED.filter(k => !process.env[k]);
if (missing.length) {
  console.warn('\n[generate-admin] Missing required environment variables — skipping admin.html generation:');
  missing.forEach(k => console.warn(`  - ${k}`));
  console.warn('\nSet them in Vercel dashboard or in a local .env.admin file.\n');
  process.exit(0);
}

// ── Replace placeholders ──────────────────────────────────────────────────────
const REPLACEMENTS = {
  '__SUPABASE_URL__':      process.env.ADMIN_SUPABASE_URL,
  '__SUPABASE_ANON__':     process.env.ADMIN_SUPABASE_ANON,
  '__ADMIN_EMAIL__':       process.env.ADMIN_EMAIL,
  '__GH_REPO__':           process.env.ADMIN_GH_REPO,
  '__GH_BRANCH__':         process.env.ADMIN_GH_BRANCH,
  '__GH_WHITELIST_PATH__': process.env.ADMIN_GH_WHITELIST_PATH,
};

const templatePath = resolve(ROOT, 'admin.template.html');
const outputPath   = resolve(ROOT, 'public', 'admin.html');

if (!existsSync(templatePath)) {
  console.error(`[generate-admin] Template not found: ${templatePath}`);
  process.exit(1);
}

let html = readFileSync(templatePath, 'utf8');

for (const [placeholder, value] of Object.entries(REPLACEMENTS)) {
  html = html.replaceAll(placeholder, value);
}

// Warn if any placeholders remain unreplaced
const remaining = html.match(/__[A-Z_]+__/g);
if (remaining) {
  console.warn('[generate-admin] Warning: unreplaced placeholders found:', [...new Set(remaining)].join(', '));
}

writeFileSync(outputPath, html, 'utf8');
console.log(`[generate-admin] ✓ public/admin.html generated from template`);
