# 🛰️ ClawBeat | The Intel Forge

**ClawBeat** is an autonomous intelligence aggregator and dispatch center for the **OpenClaw** ecosystem. It bridges the gap between raw data and actionable insights by curating news, media, and development updates across the agentic AI landscape, specifically focused on OpenClaw, Moltbot, and Steinberger developments.

---

## 🚀 Overview

ClawBeat is designed as a "High-Signal" newsroom. Unlike standard aggregators, it uses semantic intelligence to eliminate noise and prioritize primary source reporting over press release spam.

### Key Features:
* **Intel Feed (News):** Uses **Gemini 1.5** semantic embeddings to cluster related articles. It features a **"Primary Source"** logic that prioritizes authoritative journalism while lumping derivative coverage into a "More Coverage" drawer.
* **Recency Enforcement:** Headlines are strictly vetted for freshness. New articles are only granted **Priority** status if published within a **48-hour window**, ensuring the front page is always current.
* **Media Lab (Video):** A curated stream of technical demos and ecosystem updates, filtered specifically for keyword relevance.
* **The Forge (GitHub):** A real-time tracker for community repositories and agent modules, sortable by Stars or latest commits.

---

## 🛠️ The Tech Stack

* **Frontend:** React 19 + TypeScript + Tailwind CSS (Optimized for dark-mode "Intel" aesthetic)
* **Backend:** Python 3 engine (`forge.py`)
* **AI Engine:** Google Gemini API (`gemini-embedding-001`) for high-dimensional vector clustering.
* **Data Sources:** Google News RSS, YouTube Data API v3, GitHub REST API, and a curated `whitelist.json`.
* **Deployment:** Vercel

---

## 🧠 The Curation Logic (forge.py)

The heart of ClawBeat is its refined processing pipeline:
1.  **Strict Sanitization:** Automatically delists PR newswires and social media noise to maintain a professional signal.
2.  **Keyword Density Check:** Articles must meet a specific mention threshold to qualify for headline placement; single-mention "fluff" is demoted.
3.  **Semantic Clustering (0.82 Threshold):** Headlines are converted into vectors. If two stories share a similarity score of >0.82 (or 0.75 with shared specific technical keywords), they are merged to prevent feed duplication.
4.  **Referral Priority:** If a blog post refers to a major publication's story, the algorithm identifies the **Primary Source** and grants it the headline spot.

---

## 📦 Installation & Local Development

1.  **Clone the repository:**
    ```bash
    git clone [https://github.com/thekenyeung/clawbeat.git](https://github.com/thekenyeung/clawbeat.git)
    cd clawbeat
    ```

2.  **Set up Environment Variables:**
    Create a `.env` file in the root directory:
    ```env
    GEMINI_API_KEY=your_key_here
    GITHUB_TOKEN=your_token_here (optional)
    ```

3.  **Run the Forge:**
    ```bash
    python forge.py
    ```

4.  **Start the Frontend:**
    ```bash
    npm install
    npm run dev
    ```

---

## 📅 Daily Dispatches
The site organizes intel into **Daily Dispatches**. Visual dividers with high-contrast "laser" glows clearly separate the chronology, while a floating **Scroll-to-Top** feature ensures easy navigation through the 1,000-item deep archive.
