# 🤖 Moltbot News | The Intel Forge

**Moltbot News** is an autonomous intelligence aggregator and dispatch center for the **OpenClaw** ecosystem. It bridges the gap between raw data and actionable insights by curating news, media, and development updates across the agentic AI landscape.

---

## 🚀 Overview

This platform is designed to be a "living" newsroom. It doesn't just pull links; it understands the relationships between stories. 

### Key Features:
* **Intel Feed (News):** Uses **Gemini 1.5** semantic embeddings to cluster related news articles from a curated whitelist of journalism sources.
* **Media Lab (Video):** A curated stream of technical demos and ecosystem updates, filtered specifically for relevance to OpenClaw, Moltbot, and the OpenClaw Foundation.
* **The Forge (GitHub):** A real-time tracker for community repositories and agent modules, sortable by popularity (Stars) or most recent developments.

---

## 🛠️ The Tech Stack

* **Frontend:** React 19 + TypeScript + Tailwind CSS
* **Backend (The Forge):** Python 3 script (`forge.py`) 
* **AI Engine:** Google Gemini API (`gemini-embedding-001`) for semantic clustering.
* **Data Sources:** YouTube Data API v3, GitHub REST API, and various RSS/Atom feeds.
* **Deployment:** Vercel

---

## 🧠 How the "Forge" Works

The heart of this project is the `forge.py` engine. It runs a multi-stage pipeline:
1.  **Scanning:** Aggregates data from a trusted `whitelist.json`.
2.  **Clustering:** Converts headlines into high-dimensional vectors (embeddings).
3.  **Refinement:** Groups similar stories together, identifying a "Primary Source" and providing "More Coverage" links to reduce noise.
4.  **Media Curation:** Filters YouTube and GitHub feeds based on specific project keywords to ensure 100% ecosystem relevance.

---

## 📦 Installation & Local Development

1. **Clone the repository:**
   ```bash
   git clone [https://github.com/YOUR_USERNAME/moltbot-news.git](https://github.com/YOUR_USERNAME/moltbot-news.git)
   cd moltbot-news
