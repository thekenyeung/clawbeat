declare global {
  interface Window {
    gtag: (...args: any[]) => void;
  }
}

import React, { useState, useEffect } from 'react';
import { createRoot } from 'react-dom/client';
import { createClient } from '@supabase/supabase-js';

const SUPABASE_URL  = 'https://twouuiapzrkezwbtylij.supabase.co';
const SUPABASE_ANON = 'sb_publishable_j-AmOSIuQPEeKIyYAOA2Gg_8ekguDsG';
const supabase = createClient(SUPABASE_URL, SUPABASE_ANON);
import {
  Newspaper,
  Video,
  Github,
  Bot,
  ExternalLink,
  Star,
  Calendar,
  Layers,
  Award,
  ChevronLeft,
  ChevronRight,
  Menu,
  X,
  BookOpen,
  Microscope,
  MapPin,
  Globe
} from 'lucide-react';
import whitelist from './src/whitelist.json';

// --- TYPES ---
type Page = 'news' | 'videos' | 'projects' | 'research' | 'events';
type SortCriteria = 'stars' | 'date';

interface NewsItem {
  title: string;
  summary: string;
  url: string;
  source: string;
  date: string;
  source_type?: 'priority' | 'standard' | 'delist';
  moreCoverage?: Array<{ source: string; url: string }>;
  tags?: string[];
}

interface VideoItem {
  title: string;
  url: string;
  thumbnail?: string;
  channel: string;
  publishedAt: string;
  isPriority: boolean;
  description?: string;
}

interface ProjectItem {
  name: string;
  description: string;
  url: string;
  stars: number;
  owner: string;
  created_at: string;
}

interface ResearchItem {
  title: string;
  authors: string[];
  date: string;
  url: string;
  summary: string;
}

interface EventItem {
  url: string;
  title: string;
  organizer: string;
  event_type: 'virtual' | 'in-person' | 'unknown';
  location_city: string;
  location_state: string;
  location_country: string;
  start_date: string;  // MM/DD/YYYY
  end_date: string;    // MM/DD/YYYY
  description: string;
}

// --- HELPERS ---
const formatDate = (dateString: string) => {
  try {
    const date = new Date(dateString);
    if (isNaN(date.getTime())) return dateString;
    const mm = String(date.getMonth() + 1).padStart(2, '0');
    const dd = String(date.getDate()).padStart(2, '0');
    const yyyy = date.getFullYear();
    return `${mm}-${dd}-${yyyy}`;
  } catch {
    return dateString;
  }
};

const formatSourceName = (name: string) => {
  if (!name) return "";
  const rawName = name.trim();
  const key = rawName.toLowerCase().replace(/[\s\.]/g, '');

  const manualFixes: Record<string, string> = {
    "npr": "NPR", "cnbc": "CNBC", "wbur": "WBUR", "techcrunch": "TechCrunch",
    "venturebeat": "VentureBeat", "businessinsider": "Business Insider",
    "thenewstack": "The New Stack", "nytimes": "The New York Times",
    "newyorktimes": "The New York Times", "thehill": "The Hill", "wsj": "WSJ",
    "wallstreetjournal": "Wall Street Journal", "mittechnologyreview": "MIT Tech Review",
    "streetinsider": "Street Insider", "Security.Com": "Security.com",
    "Observer.Com": "Observer", "Pymnts.Com": "Pymnts", "Cnn": "CNN",
    "Cnet": "CNET", "Ibm": "IBM", "The-Decoder.com": "Decoder", "Tom'S Hardware": "Tom's Hardware"
  };

  if (manualFixes[key]) return manualFixes[key];
  if (rawName === rawName.toUpperCase() && rawName.length <= 4) return rawName;

  let cleanName = rawName.replace(/([a-z])([A-Z])/g, '$1 $2');
  if (cleanName === cleanName.toLowerCase()) {
    return cleanName.charAt(0).toUpperCase() + cleanName.slice(1);
  }
  return cleanName;
};

const checkIfVerified = (item: NewsItem) => {
  return (whitelist as any[]).some(w => {
    const whitelistName = String(w["Source Name"] || "").toLowerCase().trim();
    const articleSource = String(item.source || "").toLowerCase().trim();
    if (whitelistName === articleSource) return true;
    const whitelistUrl = String(w["Website URL"] || "").toLowerCase().replace('https://', '').replace('http://', '').replace('www.', '');
    if (whitelistUrl && item.url?.toLowerCase().includes(whitelistUrl)) return true;
    return false;
  });
};

const App: React.FC = () => {
  const urlTab = new URLSearchParams(window.location.search).get('tab') as Page | null;
  const [activePage, setActivePage] = useState<Page>(
    urlTab || (sessionStorage.getItem('activePage') as Page) || 'news'
  );
  
  const [currentPage, setCurrentPage] = useState(Number(sessionStorage.getItem('newsPage')) || 1);
  const [currentVideoPage, setCurrentVideoPage] = useState(Number(sessionStorage.getItem('videoPage')) || 1);
  const [currentProjectPage, setCurrentProjectPage] = useState(Number(sessionStorage.getItem('projectPage')) || 1);
  const [currentResearchPage, setCurrentResearchPage] = useState(Number(sessionStorage.getItem('researchPage')) || 1);
  const [currentEventsPage, setCurrentEventsPage] = useState(Number(sessionStorage.getItem('eventsPage')) || 1);

  const [sortBy, setSortBy] = useState<SortCriteria>((sessionStorage.getItem('projectSort') as SortCriteria) || 'date');
  const [isMobileMenuOpen, setIsMobileMenuOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<string>("");

  const [news, setNews] = useState<NewsItem[]>([]);
  const [videos, setVideos] = useState<VideoItem[]>([]);
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [research, setResearch] = useState<ResearchItem[]>([]);
  const [events, setEvents] = useState<EventItem[]>([]);

  const [showScrollTop, setShowScrollTop] = useState(false);

  const newsPerPage = 20;
  const videosPerPage = 9; 
  const projectsPerPage = 20;
  const researchPerPage = 10;

  useEffect(() => {
    if (window.location.search) history.replaceState(null, '', '/');
  }, []);

  useEffect(() => {
    const handleScroll = () => setShowScrollTop(window.scrollY > 800);
    window.addEventListener('scroll', handleScroll);
    return () => window.removeEventListener('scroll', handleScroll);
  }, []);

  const scrollToTop = () => window.scrollTo({ top: 0, behavior: 'smooth' });

  const trackEvent = (action: string, params: object) => {
    if (typeof window.gtag === 'function') window.gtag('event', action, params);
  };

  const handleLinkClick = (title: string, source: string, type: string = 'news_article') => {
    trackEvent('select_content', { content_type: type, item_id: title, content_source: source });
  };

  const handleNavClick = (page: Page) => {
    setActivePage(page);
    setIsMobileMenuOpen(false);
  };

  // Consolidation of Logo Click Reset Logic
  const handleLogoClick = (e?: React.MouseEvent) => {
    if (e) e.preventDefault();
    setCurrentPage(1);
    setCurrentVideoPage(1);
    setCurrentProjectPage(1);
    setCurrentResearchPage(1);
    setCurrentEventsPage(1);
    setActivePage('news');
    setIsMobileMenuOpen(false);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  useEffect(() => {
    sessionStorage.setItem('activePage', activePage);
    sessionStorage.setItem('newsPage', currentPage.toString());
    sessionStorage.setItem('videoPage', currentVideoPage.toString());
    sessionStorage.setItem('projectPage', currentProjectPage.toString());
    sessionStorage.setItem('researchPage', currentResearchPage.toString());
    sessionStorage.setItem('eventsPage', currentEventsPage.toString());
    sessionStorage.setItem('projectSort', sortBy);
  }, [activePage, currentPage, currentVideoPage, currentProjectPage, currentResearchPage, currentEventsPage, sortBy]);

  const parseMDY = (d: string) => {
    const parts = d.split('-').map(Number);
    const [m, day, y] = [parts[0] ?? 0, parts[1] ?? 0, parts[2] ?? 0];
    return isNaN(y) || y === 0 ? 0 : new Date(y, m - 1, day).getTime();
  };

  const fetchContent = async () => {
    setLoading(true);
    try {
      const [newsRes, videosRes, projectsRes, researchRes, eventsRes, metaRes] = await Promise.all([
        supabase.from('news_items').select('*').limit(1000),
        supabase.from('videos').select('*').limit(300),
        supabase.from('github_projects').select('*').limit(100),
        supabase.from('research_papers').select('*').limit(100),
        supabase.from('events').select('*').limit(500),
        supabase.from('feed_metadata').select('*').eq('id', 1).maybeSingle(),
      ]);

      if (newsRes.error) throw newsRes.error;
      if (videosRes.error) throw videosRes.error;
      if (projectsRes.error) throw projectsRes.error;
      if (researchRes.error) throw researchRes.error;

      setLastUpdated(metaRes.data?.last_updated || '');

      // Map DB snake_case → frontend camelCase
      setNews((newsRes.data || []).map((item: any) => ({
        ...item,
        moreCoverage: item.more_coverage || [],
        tags: item.tags || [],
      })));

      setVideos((videosRes.data || [])
        .map((v: any) => ({ ...v, publishedAt: v.published_at || '' }))
        .sort((a: any, b: any) => parseMDY(b.publishedAt) - parseMDY(a.publishedAt)));

      setProjects(projectsRes.data || []);
      setResearch(researchRes.data || []);
      setEvents(eventsRes.data || []);
    } catch (err: any) {
      setError("Intelligence feed is currently updating...");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchContent();
  }, []);

  const sortedProjects = [...projects].sort((a, b) => 
    sortBy === 'stars' 
      ? (b.stars || 0) - (a.stars || 0) 
      : new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
  );

  const sortedResearch = [...research].sort((a, b) => 
    new Date(b.date).getTime() - new Date(a.date).getTime()
  );

  const currentNewsItems = news.slice((currentPage - 1) * newsPerPage, currentPage * newsPerPage);
  const totalNewsPages = Math.ceil(news.length / newsPerPage);

  const currentVideoItems = videos.slice((currentVideoPage - 1) * videosPerPage, currentVideoPage * videosPerPage);
  const totalVideoPages = Math.ceil(videos.length / videosPerPage);

  const currentProjectItems = sortedProjects.slice((currentProjectPage - 1) * projectsPerPage, currentProjectPage * projectsPerPage);
  const totalProjectPages = Math.ceil(sortedProjects.length / projectsPerPage);

  const currentResearchItems = sortedResearch.slice((currentResearchPage - 1) * researchPerPage, currentResearchPage * researchPerPage);
  const totalResearchPages = Math.ceil(sortedResearch.length / researchPerPage);

  // Events: filter past (end_date < today) client-side, sort nearest upcoming first
  const eventsPerPage = 20;
  const parseMMDDYYYY = (d: string) => {
    const p = d.split('/').map(Number);
    return p.length === 3 ? new Date(p[2] ?? 0, (p[0] ?? 1) - 1, p[1] ?? 1) : new Date(0);
  };
  const todayMidnight = new Date(); todayMidnight.setHours(0, 0, 0, 0);
  const upcomingEvents = [...events]
    .filter(e => parseMMDDYYYY(e.end_date || e.start_date) >= todayMidnight)
    .sort((a, b) => parseMMDDYYYY(a.start_date).getTime() - parseMMDDYYYY(b.start_date).getTime());
  const currentEventItems = upcomingEvents.slice((currentEventsPage - 1) * eventsPerPage, currentEventsPage * eventsPerPage);
  const totalEventsPages = Math.ceil(upcomingEvents.length / eventsPerPage);

  return (
    <div className="min-h-screen bg-[#0a0a0c] text-slate-200 font-sans selection:bg-orange-500/30 selection:text-orange-200">
      <header className="sticky top-0 z-50 border-b border-white/5 bg-[#0a0a0c]/80 backdrop-blur-xl">
        <div className="max-w-6xl mx-auto px-4 h-16 flex items-center justify-between">
          <div className="flex items-center gap-3 cursor-pointer group" onClick={handleLogoClick}>
            <div className="w-10 h-10 rounded-lg overflow-hidden border border-white/10 group-hover:border-orange-500/50 transition-all shadow-2xl">
              <img src="/images/clawbeat-icon-claw-logo-512x512.jpg" alt="Logo" className="w-full h-full object-cover" />
            </div>
            <h1 className="text-xl font-black text-white uppercase italic tracking-tighter">
              ClawBeat<span className="text-orange-500">.co</span>
            </h1>
          </div>
          <nav className="hidden md:flex items-center gap-1">
            <NavButton active={activePage === 'news'} onClick={() => handleNavClick('news')} icon={<Newspaper className="w-4 h-4" />} label="Intel" />
            <NavButton active={false} onClick={() => window.location.href = '/research.html'} icon={<BookOpen className="w-4 h-4" />} label="Research" />
            <NavButton active={false} onClick={() => window.location.href = '/media.html'} icon={<Video className="w-4 h-4" />} label="Media" />
            <NavButton active={false} onClick={() => window.location.href = '/forge.html'} icon={<Github className="w-4 h-4" />} label="Forge" />
            <NavButton active={false} onClick={() => window.location.href = '/events-calendar.html'} icon={<Calendar className="w-4 h-4" />} label="Events" />
          </nav>
          <button onClick={() => setIsMobileMenuOpen(!isMobileMenuOpen)} className="md:hidden p-2 text-slate-400 hover:text-white">
            {isMobileMenuOpen ? <X className="w-6 h-6" /> : <Menu className="w-6 h-6" />}
          </button>
        </div>
        {isMobileMenuOpen && (
          <div className="md:hidden absolute top-16 left-0 w-full bg-[#0a0a0c]/95 backdrop-blur-lg border-b border-white/10 py-4 px-4 flex flex-col gap-2 animate-in fade-in slide-in-from-top-2 duration-200 z-[60]">
            <NavButton active={activePage === 'news'} onClick={() => handleNavClick('news')} icon={<Newspaper className="w-4 h-4" />} label="Intel Feed" />
            <NavButton active={false} onClick={() => window.location.href = '/research.html'} icon={<BookOpen className="w-4 h-4" />} label="Research" />
            <NavButton active={false} onClick={() => window.location.href = '/media.html'} icon={<Video className="w-4 h-4" />} label="Media Lab" />
            <NavButton active={false} onClick={() => window.location.href = '/forge.html'} icon={<Github className="w-4 h-4" />} label="The Forge" />
            <NavButton active={false} onClick={() => window.location.href = '/events-calendar.html'} icon={<Calendar className="w-4 h-4" />} label="Events" />
          </div>
        )}
      </header>

      <main className="max-w-6xl mx-auto px-4 py-8">
        <div className="flex justify-between items-end mb-12 border-b border-white/5 pb-8">
          <div>
            <h2 className="text-4xl font-black text-white uppercase italic tracking-tighter leading-[1.1]">
              {activePage === 'news' && <>Ecosystem <span className="text-orange-500">Dispatch</span></>}
              {activePage === 'research' && <>Technical <span className="text-orange-500">Papers</span></>}
              {activePage === 'videos' && <>Visual <span className="text-orange-500">Stream</span></>}
              {activePage === 'projects' && <>The <span className="text-orange-500">Forge</span></>}
              {activePage === 'events' && <>Community <span className="text-orange-500">Events</span></>}
            </h2>
            <div className="flex flex-col gap-1 mt-2">
              <p className="text-slate-500 text-xs uppercase font-black tracking-[0.2em]">
                {activePage === 'research' ? 'ArXiv Intelligence & Semantic Scholar' : activePage === 'events' ? 'Upcoming OpenClaw Gatherings' : 'Autonomous Intelligence Curation'}
              </p>
              {lastUpdated && (
                <span className="text-[10px] font-black text-orange-500/60 uppercase tracking-widest whitespace-nowrap">
                  Last Sync: {lastUpdated}
                </span>
              )}
            </div>
          </div>
          {activePage === 'projects' && (
            <div className="flex gap-2 bg-white/5 p-1 rounded-lg">
              <SortButton active={sortBy === 'stars'} onClick={() => setSortBy('stars')} label="Top Rated" />
              <SortButton active={sortBy === 'date'} onClick={() => setSortBy('date')} label="Latest" />
            </div>
          )}
        </div>

        {loading ? (
          <div className="space-y-6 animate-pulse">
            {[...Array(6)].map((_, i) => <div key={i} className="h-24 bg-white/5 rounded-lg" />)}
          </div>
        ) : error ? (
          <div className="bg-red-500/10 border border-red-500/20 p-8 rounded-xl text-center">
            <p className="text-red-400 font-mono text-sm">{error}</p>
            <button onClick={() => fetchContent()} className="mt-4 text-xs text-slate-500 underline uppercase tracking-widest">Retry Sync</button>
          </div>
        ) : (
          <div className="min-h-[50vh]">
            {activePage === 'news' && (
              <>
                <NewsList items={currentNewsItems} onTrackClick={handleLinkClick} />
                <Pagination current={currentPage} total={totalNewsPages} onChange={setCurrentPage} />
              </>
            )}
            {activePage === 'research' && (
              <>
                <ResearchList items={currentResearchItems} onTrackClick={handleLinkClick} />
                <Pagination current={currentResearchPage} total={totalResearchPages} onChange={setCurrentResearchPage} />
              </>
            )}
            {activePage === 'videos' && (
              <>
                <VideoGrid items={currentVideoItems} onTrackClick={handleLinkClick} />
                <Pagination current={currentVideoPage} total={totalVideoPages} onChange={setCurrentVideoPage} />
              </>
            )}
            {activePage === 'projects' && (
              <>
                <ProjectGrid items={currentProjectItems} onTrackClick={handleLinkClick} />
                <Pagination current={currentProjectPage} total={totalProjectPages} onChange={setCurrentProjectPage} />
              </>
            )}
            {activePage === 'events' && (
              <>
                <EventsList items={currentEventItems} total={upcomingEvents.length} onTrackClick={handleLinkClick} />
                <Pagination current={currentEventsPage} total={totalEventsPages} onChange={setCurrentEventsPage} />
              </>
            )}
          </div>
        )}
      </main>

      <button
        onClick={scrollToTop}
        className={`fixed bottom-8 right-8 p-4 rounded-xl bg-orange-600 text-white shadow-[0_0_25px_rgba(234,88,12,0.4)] transition-all duration-300 z-[100] hover:scale-110 active:scale-95 border border-orange-400/50 ${
          showScrollTop ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-12 pointer-events-none'
        }`}
      >
        <ChevronLeft className="w-6 h-6 rotate-90" />
      </button>

      <footer className="border-t border-white/5 bg-[#0d0f12] py-5 mt-8">
        <div className="max-w-6xl mx-auto px-4 flex items-center justify-between gap-4 flex-wrap">
          <div className="text-sm font-black uppercase italic tracking-tight text-white">
            ClawBeat<span className="text-orange-500">.co</span>
          </div>
          <div className="font-mono text-[10px] text-slate-600">signal_dispatch // intel_feed</div>
        </div>
      </footer>
    </div>
  );
};

// ... Remaining Components (Pagination, NavButton, SortButton, NewsList, ResearchList, VideoGrid, ProjectGrid)
// Ensure they stay exactly as they were in your functional version.

const Pagination = ({ current, total, onChange }: { current: number; total: number; onChange: (p: number) => void }) => {
  if (total <= 1) return null;
  const handlePageChange = (newPage: number) => {
    onChange(newPage);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  return (
    <div className="flex justify-center items-center gap-6 mt-16 pt-12 border-t border-white/5">
      <button disabled={current === 1} onClick={() => handlePageChange(current - 1)} className="group relative flex items-center gap-2 px-6 py-3 text-[10px] font-black uppercase tracking-[0.2em] bg-white/5 hover:bg-orange-500/10 disabled:opacity-20 rounded-xl transition-all border border-white/5 hover:border-orange-500/30">
        <ChevronLeft className="w-4 h-4 text-orange-500" />
        <span>Prev</span>
      </button>
      <div className="flex flex-col items-center">
        <span className="text-[10px] font-black text-slate-500 uppercase tracking-[0.5em] mb-1">Page</span>
        <div className="flex items-center gap-2">
          <span className="text-lg font-black text-white italic">{current}</span>
          <span className="text-orange-500/30 text-xs">/</span>
          <span className="text-xs font-bold text-slate-500">{total}</span>
        </div>
      </div>
      <button disabled={current === total} onClick={() => handlePageChange(current + 1)} className="group relative flex items-center gap-2 px-6 py-3 text-[10px] font-black uppercase tracking-[0.2em] bg-white/5 hover:bg-orange-500/10 disabled:opacity-20 rounded-xl transition-all border border-white/5 hover:border-orange-500/30 shadow-[0_0_20px_rgba(249,115,22,0.1)]">
        <span>Next</span>
        <ChevronRight className="w-4 h-4 text-orange-500" />
      </button>
    </div>
  );
};

const NavButton = ({ active, onClick, icon, label }: any) => (
  <button onClick={onClick} className={`flex items-center gap-3 px-4 py-3 md:py-1.5 rounded-md text-xs md:text-[10px] font-black uppercase tracking-widest transition-all w-full md:w-auto ${active ? 'bg-white/10 text-orange-500 shadow-[inset_0_0_10px_rgba(249,115,22,0.1)]' : 'text-slate-500 hover:text-slate-300 hover:bg-white/5'}`}>
    {icon} {label}
  </button>
);

const SortButton = ({ active, onClick, label }: any) => (
  <button onClick={onClick} className={`px-3 py-1 text-[10px] font-black uppercase rounded transition-colors ${active ? 'bg-orange-600 text-white' : 'text-slate-500 hover:bg-white/5'}`}>
    {label}
  </button>
);

const NewsList = ({ items, onTrackClick }: { items: NewsItem[], onTrackClick: (t: string, s: string) => void }) => {
  const sortedByPriority = [...items].sort((a, b) => {
    const aVerified = checkIfVerified(a);
    const bVerified = checkIfVerified(b);
    if (aVerified !== bVerified) return aVerified ? -1 : 1;
    const priorityWeight = { priority: 1, standard: 2, delist: 3 };
    const aWeight = priorityWeight[a.source_type || 'standard'];
    const bWeight = priorityWeight[b.source_type || 'standard'];
    if (aWeight !== bWeight) return aWeight - bWeight;
    return 0;
  });

  const grouped = sortedByPriority.reduce((acc: Record<string, NewsItem[]>, item) => {
    const date = item.date || "recent";
    if (!acc[date]) acc[date] = [];
    acc[date].push(item);
    return acc;
  }, {});

  return (
    <div className="flex flex-col">
      {Object.keys(grouped).sort((a, b) => new Date(b).getTime() - new Date(a).getTime()).map((date) => (
        <React.Fragment key={date}>
          <div className="flex items-center gap-6 my-12 first:mt-0 relative overflow-hidden">
            <div className="h-[2px] w-12 bg-gradient-to-r from-transparent to-orange-500 shadow-[0_0_10px_rgba(249,115,22,0.5)]" />
            <h3 className="text-[11px] font-black text-orange-500 uppercase tracking-[0.5em] whitespace-nowrap bg-black px-2 relative z-10">
              Dispatch: <span className="text-white">{date}</span>
            </h3>
            <div className="h-[1px] flex-grow bg-gradient-to-r from-orange-500/50 via-orange-500/10 to-transparent relative">
              <div className="absolute inset-0 bg-orange-500/20 blur-sm" />
            </div>
          </div>
          {grouped[date]?.map((item, idx) => {
            const isVerified = checkIfVerified(item);
            const isPriority = item.source_type === 'priority';
            return (
              <div key={idx} className="flex flex-col gap-3 py-6 border-b border-white/5 last:border-0 group">
                <a href={item.url} target="_blank" rel="noopener noreferrer" onClick={() => onTrackClick(item.title, item.source)} className="text-xl font-bold text-white group-hover:text-orange-500 leading-tight transition-colors flex items-start gap-2">
                  <span className="flex-1">{item.title}</span>
                  <ExternalLink className="w-4 h-4 mt-1.5 opacity-0 group-hover:opacity-40 transition-opacity flex-shrink-0" />
                </a>
                <div className="flex items-center gap-3">
                  <span className="text-[10px] font-black text-slate-500 uppercase tracking-widest">{formatSourceName(item.source)}</span>
                  {isVerified && <span className="text-[8px] font-black text-orange-500 bg-orange-500/10 px-2 py-0.5 rounded uppercase tracking-tighter flex items-center gap-1"><Bot className="w-2.5 h-2.5" /> verified</span>}
                  {isPriority && !isVerified && <span className="text-[8px] font-black text-slate-400 border border-white/10 px-2 py-0.5 rounded uppercase tracking-tighter flex items-center gap-1"><Award className="w-2.5 h-2.5" /> priority feed</span>}
                </div>
                {item.tags && item.tags.length > 0 && (
                  <div className="flex flex-wrap gap-1.5">
                    {item.tags.map((tag, tIdx) => (
                      <span key={tIdx} className="text-[9px] font-black text-slate-500 bg-white/[0.04] border border-white/[0.06] px-2 py-0.5 rounded uppercase tracking-wider">
                        {tag}
                      </span>
                    ))}
                  </div>
                )}
                {item.summary && <p className="text-slate-400 text-sm leading-relaxed line-clamp-3">{item.summary}</p>}
                {item.moreCoverage && item.moreCoverage.length > 0 && (
                  <div className="mt-2 flex flex-col gap-2">
                    <div className="flex items-center gap-2">
                      <Layers className="w-3 h-3 text-slate-600" />
                      <span className="text-[9px] font-black text-slate-600 uppercase tracking-widest">More Coverage</span>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {item.moreCoverage
                        .filter(link => !link.source.toLowerCase().includes('facebook'))
                        .map((link, lIdx) => (
                          <a key={lIdx} href={link.url} target="_blank" rel="noopener noreferrer" onClick={() => onTrackClick(item.title, link.source)} className="text-[10px] font-bold text-orange-500/80 bg-orange-500/5 hover:bg-orange-500/10 border border-orange-500/10 px-2 py-1 rounded transition-all">
                            {formatSourceName(link.source)}
                          </a>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </React.Fragment>
      ))}
    </div>
  );
};

const ResearchList = ({ items, onTrackClick }: { items: ResearchItem[], onTrackClick: (t: string, s: string) => void }) => (
  <div className="flex flex-col gap-8">
    {items.map((paper, idx) => (
      <div key={idx} className="group border-l-2 border-white/5 hover:border-orange-500/50 pl-6 py-4 transition-all bg-white/[0.01] hover:bg-white/[0.03] rounded-r-xl relative">
        <a href={paper.url} target="_blank" rel="noopener noreferrer" onClick={() => onTrackClick(paper.title, 'ArXiv')} className="flex items-start justify-between gap-4">
          <h3 className="text-xl font-bold text-white group-hover:text-orange-500 transition-colors leading-tight pr-12">
            {paper.title}
          </h3>
          <ExternalLink className="w-5 h-5 mt-1 text-slate-600 group-hover:text-orange-500 transition-colors absolute right-6 top-6" />
        </a>
        <div className="flex flex-wrap items-center gap-3 mt-3">
          <div className="flex items-center gap-2 text-[10px] font-black text-slate-500 uppercase tracking-widest">
            <Microscope className="w-3 h-3 text-orange-500/50" />
            <span>{formatDate(paper.date)}</span>
          </div>
          <span className="text-white/10">|</span>
          <div className="text-[10px] font-bold text-slate-400 uppercase truncate max-w-xl italic tracking-tight">
            Authored by: {paper.authors.join(', ')}
          </div>
        </div>
        <p className="mt-4 text-sm text-slate-400 leading-relaxed italic border-t border-white/5 pt-4">
          <span className="text-orange-500/50 not-italic font-black text-[10px] uppercase tracking-widest mr-3">Intel Brief:</span>
          {paper.summary}
        </p>
      </div>
    ))}
  </div>
);

const VideoGrid = ({ items, onTrackClick }: { items: VideoItem[], onTrackClick: (t: string, s: string, type: string) => void }) => (
  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-10">
    {items.map((video, idx) => (
      <div key={idx} className="group relative flex flex-col">
        <div className="relative aspect-video rounded-xl overflow-hidden mb-4 ring-1 ring-white/10 group-hover:ring-orange-500/50 transition-all shadow-lg">
          {video.thumbnail ? (
            <img src={video.thumbnail} className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500" alt={video.title} />
          ) : (
            <div className="w-full h-full bg-white/5 flex items-center justify-center"><span className="text-white/20 text-xs uppercase tracking-widest font-black">No Preview</span></div>
          )}
        </div>
        <h4 className="font-bold text-white text-lg group-hover:text-orange-500 line-clamp-2 leading-tight">{video.title}</h4>
        <p className="text-[10px] text-orange-500 mt-2 uppercase font-black tracking-widest">{video.channel} • {formatDate(video.publishedAt)}</p>
        {video.description && <p className="text-slate-400 text-xs mt-3 line-clamp-2 leading-relaxed italic">{video.description}</p>}
        <a href={video.url} target="_blank" rel="noopener noreferrer" onClick={() => onTrackClick(video.title, video.channel, 'video')} className="absolute inset-0 z-10" />
      </div>
    ))}
  </div>
);

const ProjectGrid = ({ items, onTrackClick }: { items: ProjectItem[], onTrackClick: (t: string, s: string, type: string) => void }) => (
  <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
    {items.map((p, idx) => (
      <div key={idx} className="group relative p-6 rounded-xl bg-white/[0.03] border border-white/5 hover:border-orange-500/40 transition-all flex flex-col justify-between shadow-sm">
        <div>
          <div className="flex justify-between items-start mb-4">
            <h4 className="text-lg font-bold text-white group-hover:text-orange-500 transition-colors tracking-tight">{p.name}</h4>
            <div className="flex items-center gap-1 bg-orange-500/10 px-2 py-1 rounded">
              <Star className="w-3 h-3 text-orange-500 fill-orange-500" />
              <span className="text-orange-500 font-black text-xs">{p.stars?.toLocaleString() ?? 0}</span>
            </div>
          </div>
          <p className="text-slate-400 text-sm leading-relaxed line-clamp-3 italic mb-6">{p.description}</p>
        </div>
        <div className="flex items-center justify-between mt-auto pt-4 border-t border-white/5">
          <div className="flex flex-col">
            <span className="text-[10px] text-slate-500 uppercase font-black tracking-widest">Author</span>
            <span className="text-xs text-white font-bold">{p.owner}</span>
          </div>
          <div className="flex flex-col text-right">
            <span className="text-[10px] text-slate-500 uppercase font-black tracking-widest">Created</span>
            <span className="text-xs text-white font-bold flex items-center gap-1"><Calendar className="w-3 h-3 text-orange-500/50" /> {formatDate(p.created_at)}</span>
          </div>
        </div>
        <a href={p.url} target="_blank" rel="noopener noreferrer" onClick={() => onTrackClick(p.name, p.owner, 'github_repo')} className="absolute inset-0 z-10" />
      </div>
    ))}
  </div>
);

const EventsList = ({ items, total, onTrackClick }: { items: EventItem[], total: number, onTrackClick: (t: string, s: string) => void }) => {
  if (total === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-32 gap-6 text-center">
        <Calendar className="w-12 h-12 text-white/10" />
        <div>
          <p className="text-white font-black text-xl uppercase italic tracking-tight">No Upcoming Events</p>
          <p className="text-slate-500 text-sm mt-2 max-w-sm">Events are discovered automatically each day. Check back soon.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {items.map((event, idx) => {
        const isVirtual = event.event_type === 'virtual';
        const locationParts = [event.location_city, event.location_state, event.location_country].filter(Boolean);
        const locationStr = locationParts.join(', ');
        const dateStr = event.start_date === event.end_date || !event.end_date
          ? event.start_date
          : `${event.start_date} – ${event.end_date}`;

        return (
          <div key={idx} className="group relative p-6 rounded-xl bg-white/[0.02] border border-white/5 hover:border-orange-500/30 transition-all flex flex-col gap-3">
            <div className="flex items-center gap-3 flex-wrap">
              {isVirtual ? (
                <span className="flex items-center gap-1.5 text-[9px] font-black text-orange-500 bg-orange-500/10 px-2 py-0.5 rounded uppercase tracking-wider">
                  <Globe className="w-2.5 h-2.5" /> Virtual
                </span>
              ) : (
                <span className="flex items-center gap-1.5 text-[9px] font-black text-slate-400 bg-white/5 border border-white/10 px-2 py-0.5 rounded uppercase tracking-wider">
                  <MapPin className="w-2.5 h-2.5" /> In-Person
                </span>
              )}
              {locationStr && !isVirtual && (
                <span className="text-[10px] font-black text-slate-500 uppercase tracking-wider">{locationStr}</span>
              )}
              {dateStr && (
                <span className="text-[10px] font-black text-slate-600 uppercase tracking-wider ml-auto">{dateStr}</span>
              )}
            </div>

            <a href={event.url} target="_blank" rel="noopener noreferrer" onClick={() => onTrackClick(event.title, event.organizer)} className="text-xl font-bold text-white group-hover:text-orange-500 leading-tight transition-colors flex items-start gap-2">
              <span className="flex-1">{event.title}</span>
              <ExternalLink className="w-4 h-4 mt-1.5 opacity-0 group-hover:opacity-40 transition-opacity flex-shrink-0" />
            </a>

            {event.organizer && (
              <span className="text-[10px] font-black text-slate-500 uppercase tracking-widest">{event.organizer}</span>
            )}
            {event.description && (
              <p className="text-slate-400 text-sm leading-relaxed line-clamp-3">{event.description}</p>
            )}
          </div>
        );
      })}
    </div>
  );
};

const container = document.getElementById('root');
if (container) createRoot(container).render(<App />);