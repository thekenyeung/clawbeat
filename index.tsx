declare global {
  interface Window {
    gtag: (...args: any[]) => void;
  }
}

import React, { useState, useEffect } from 'react';
import { createRoot } from 'react-dom/client';
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
  ChevronRight
} from 'lucide-react';
import whitelist from './src/whitelist.json';

// --- TYPES ---
type Page = 'news' | 'videos' | 'projects';
type SortCriteria = 'stars' | 'date';

interface NewsItem {
  title: string;
  summary: string;
  url: string;
  source: string;
  date: string;
  source_type?: 'priority' | 'standard' | 'delist';
  moreCoverage?: Array<{ source: string; url: string }>;
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
  let cleanName = name.replace(/([a-z])([A-Z])/g, '$1 $2');
  const manualFixes: Record<string, string> = {
    "businessinsider": "Business Insider",
    "venturebeat": "VentureBeat",
    "thenewstack": "The New Stack",
    "csoonline": "CSO Online",
    "americanbanker": "American Banker",
    "institutionalinvestor": "Institutional Investor",
    "fastcompany": "Fast Company"
  };
  const key = cleanName.toLowerCase().replace(/\s+/g, '');
  return manualFixes[key] || cleanName;
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
  const [activePage, setActivePage] = useState<Page>('news');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<string>("");
  const [sortBy, setSortBy] = useState<SortCriteria>('date');

  const [news, setNews] = useState<NewsItem[]>([]);
  const [videos, setVideos] = useState<VideoItem[]>([]);
  const [projects, setProjects] = useState<ProjectItem[]>([]);

  // Independent Pagination States
  const [currentPage, setCurrentPage] = useState(1);
  const [currentVideoPage, setCurrentVideoPage] = useState(1);
  
  const newsPerPage = 20;
  const videosPerPage = 9; // 3x3 grid
  const projectsPerPage = 20;

  const trackEvent = (action: string, params: object) => {
    if (typeof window.gtag === 'function') {
      window.gtag('event', action, params);
    }
  };

  const handleLinkClick = (title: string, source: string, type: string = 'news_article') => {
    trackEvent('select_content', {
      content_type: type,
      item_id: title,
      content_source: source
    });
  };

  useEffect(() => {
    trackEvent('page_view', {
      page_title: activePage.charAt(0).toUpperCase() + activePage.slice(1),
      page_location: window.location.href,
      page_path: `/${activePage}`
    });
    // We don't reset to 1 here anymore to preserve tab state, 
    // unless you prefer it to reset every time.
  }, [activePage]);

  const fetchContent = async (page: Page) => {
    setLoading(true);
    try {
      const GITHUB_RAW_URL = "https://raw.githubusercontent.com/thekenyeung/moltbot-news/main/public/data.json";
      const response = await fetch(`${GITHUB_RAW_URL}?t=${new Date().getTime()}`);
      if (!response.ok) throw new Error("Could not find data.json.");
      
      const allData = await response.json();
      setLastUpdated(allData.last_updated || "");
      
      if (page === 'news') setNews(allData.items || []);
      if (page === 'videos') setVideos(allData.videos || []);
      if (page === 'projects') setProjects(allData.githubProjects || []);
    } catch (err: any) {
      setError("Intelligence feed is currently updating...");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchContent(activePage);
  }, [activePage]);

  // Sorting Logic
  const sortedProjects = [...projects].sort((a, b) => 
    sortBy === 'stars' 
      ? (b.stars || 0) - (a.stars || 0) 
      : new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
  );

  const sortedVideos = [...videos].sort((a, b) => {
    if (a.isPriority !== b.isPriority) return a.isPriority ? -1 : 1;
    return new Date(b.publishedAt).getTime() - new Date(a.publishedAt).getTime();
  });

  // News Pagination Math
  const indexOfLastNews = currentPage * newsPerPage;
  const currentNewsItems = news.slice(indexOfLastNews - newsPerPage, indexOfLastNews);
  const totalNewsPages = Math.ceil(news.length / newsPerPage);

  // Video Pagination Math
  const indexOfLastVideo = currentVideoPage * videosPerPage;
  const currentVideoItems = sortedVideos.slice(indexOfLastVideo - videosPerPage, indexOfLastVideo);
  const totalVideoPages = Math.ceil(sortedVideos.length / videosPerPage);

  // Project Pagination Math
  const indexOfLastProject = currentPage * projectsPerPage;
  const currentProjectItems = sortedProjects.slice(indexOfLastProject - projectsPerPage, indexOfLastProject);
  const totalProjectPages = Math.ceil(sortedProjects.length / projectsPerPage);

  return (
    <div className="min-h-screen bg-[#0a0a0c] text-slate-200 font-sans selection:bg-orange-500/30 selection:text-orange-200">
      <header className="sticky top-0 z-50 border-b border-white/5 bg-[#0a0a0c]/80 backdrop-blur-xl">
        <div className="max-w-6xl mx-auto px-4 h-16 flex items-center justify-between">
          <div className="flex items-center gap-3 cursor-pointer group" onClick={() => setActivePage('news')}>
            <div className="w-10 h-10 rounded-lg overflow-hidden border border-white/10 group-hover:border-orange-500/50 transition-all shadow-2xl">
              <img 
                src="/images/moltbot-news-robot-orange-box-512x512.jpg" 
                alt="Moltbot Logo"
                className="w-full h-full object-cover"
              />
            </div>
            <h1 className="text-xl font-black text-white uppercase italic tracking-tighter">
              Moltbot <span className="text-orange-500">News</span>
            </h1>
          </div>
          <nav className="flex items-center gap-1">
            <NavButton active={activePage === 'news'} onClick={() => setActivePage('news')} icon={<Newspaper className="w-4 h-4" />} label="Intel Feed" />
            <NavButton active={activePage === 'videos'} onClick={() => setActivePage('videos')} icon={<Video className="w-4 h-4" />} label="Media Lab" />
            <NavButton active={activePage === 'projects'} onClick={() => setActivePage('projects')} icon={<Github className="w-4 h-4" />} label="The Forge" />
          </nav>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-4 py-8">
        <div className="flex justify-between items-end mb-12 border-b border-white/5 pb-8">
          <div>
            <h2 className="text-4xl font-black text-white uppercase italic tracking-tighter">
              {activePage === 'news' && 'Ecosystem Dispatch'}
              {activePage === 'videos' && 'Visual Stream'}
              {activePage === 'projects' && 'The Forge'}
            </h2>
            <div className="flex flex-col gap-1 mt-2">
              <p className="text-slate-500 text-xs uppercase font-black tracking-[0.2em]">
                {activePage === 'projects' ? 'Community Repositories' : 'Autonomous Intelligence Curation'}
              </p>
              {lastUpdated && activePage === 'news' && (
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
            <button onClick={() => fetchContent(activePage)} className="mt-4 text-xs text-slate-500 underline uppercase tracking-widest">Retry Sync</button>
          </div>
        ) : (
          <div className="min-h-[50vh]">
            {activePage === 'news' && (
              <>
                <NewsList items={currentNewsItems} onTrackClick={handleLinkClick} />
                <Pagination current={currentPage} total={totalNewsPages} onChange={setCurrentPage} />
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
                <Pagination current={currentPage} total={totalProjectPages} onChange={setCurrentPage} />
              </>
            )}
          </div>
        )}
      </main>
    </div>
  );
};

// --- COMPONENTS ---

const Pagination = ({ current, total, onChange }: { current: number; total: number; onChange: (p: number) => void }) => {
  if (total <= 1) return null;

  const handlePageChange = (newPage: number) => {
    // 1. Trigger the state change
    onChange(newPage);
    
    // 2. Smoothly glide back to the top of the feed
    window.scrollTo({
      top: 0,
      behavior: 'smooth'
    });
  };

  return (
    <div className="flex justify-center items-center gap-6 mt-16 pt-12 border-t border-white/5">
      <button 
        disabled={current === 1}
        onClick={() => handlePageChange(current - 1)}
        className="group relative flex items-center gap-2 px-6 py-3 text-[10px] font-black uppercase tracking-[0.2em] bg-white/5 hover:bg-orange-500/10 disabled:opacity-20 disabled:hover:bg-white/5 rounded-xl transition-all border border-white/5 hover:border-orange-500/30 overflow-hidden"
      >
        <div className="absolute inset-0 bg-orange-500/10 translate-y-full group-hover:translate-y-0 transition-transform duration-300" />
        <ChevronLeft className="w-4 h-4 text-orange-500 relative z-10" />
        <span className="relative z-10">Prev</span>
      </button>

      <div className="flex flex-col items-center">
        <span className="text-[10px] font-black text-slate-500 uppercase tracking-[0.5em] mb-1">
          Stream
        </span>
        <div className="flex items-center gap-2">
          <span className="text-lg font-black text-white italic">{current}</span>
          <span className="text-orange-500/30 text-xs">/</span>
          <span className="text-xs font-bold text-slate-500">{total}</span>
        </div>
      </div>

      <button 
        disabled={current === total}
        onClick={() => handlePageChange(current + 1)}
        className="group relative flex items-center gap-2 px-6 py-3 text-[10px] font-black uppercase tracking-[0.2em] bg-white/5 hover:bg-orange-500/10 disabled:opacity-20 disabled:hover:bg-white/5 rounded-xl transition-all border border-white/5 hover:border-orange-500/30 overflow-hidden shadow-[0_0_20px_rgba(249,115,22,0)] hover:shadow-[0_0_20px_rgba(249,115,22,0.1)]"
      >
        <div className="absolute inset-0 bg-orange-500/10 translate-y-full group-hover:translate-y-0 transition-transform duration-300" />
        <span className="relative z-10">Next</span>
        <ChevronRight className="w-4 h-4 text-orange-500 relative z-10" />
      </button>
    </div>
  );
};

// ... (Rest of components: NavButton, SortButton, NewsList, VideoGrid, ProjectGrid)

const NavButton = ({ active, onClick, icon, label }: any) => (
  <button onClick={onClick} className={`flex items-center gap-2 px-4 py-1.5 rounded-md text-[10px] font-black uppercase tracking-widest transition-all ${active ? 'bg-white/10 text-orange-500' : 'text-slate-500 hover:text-slate-300'}`}>
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
          <div className="flex items-center gap-4 my-8 first:mt-0">
            <h3 className="text-[10px] font-black text-orange-500 uppercase tracking-[0.4em] whitespace-nowrap">
              Dispatch: {date}
            </h3>
            <div className="h-[1px] w-full bg-orange-500/10" />
          </div>

          {grouped[date]?.map((item, idx) => {
            const isVerified = checkIfVerified(item);
            const isPriority = item.source_type === 'priority';

            return (
              <div key={idx} className="flex flex-col gap-3 py-6 border-b border-white/5 last:border-0 group">
                <a 
                  href={item.url} 
                  target="_blank" 
                  rel="noopener noreferrer" 
                  onClick={() => onTrackClick(item.title, item.source)}
                  className="text-xl font-bold text-white group-hover:text-orange-500 leading-tight transition-colors flex items-start gap-2"
                >
                  <span className="flex-1">{item.title}</span>
                  <ExternalLink className="w-4 h-4 mt-1.5 opacity-0 group-hover:opacity-40 transition-opacity flex-shrink-0" />
                </a>
                
                <div className="flex items-center gap-3">
                  <span className="text-[10px] font-black text-slate-500 uppercase tracking-widest">
                    {formatSourceName(item.source)}
                  </span>
                  
                  {isVerified && (
                    <span className="text-[8px] font-black text-orange-500 bg-orange-500/10 px-2 py-0.5 rounded uppercase tracking-tighter flex items-center gap-1">
                      <Bot className="w-2.5 h-2.5" /> verified
                    </span>
                  )}

                  {isPriority && !isVerified && (
                    <span className="text-[8px] font-black text-slate-400 border border-white/10 px-2 py-0.5 rounded uppercase tracking-tighter flex items-center gap-1">
                      <Award className="w-2.5 h-2.5" /> priority feed
                    </span>
                  )}
                </div>

                {item.summary && (
                  <p className="text-slate-400 text-sm leading-relaxed line-clamp-3">
                    {item.summary}
                  </p>
                )}

                {item.moreCoverage && item.moreCoverage.length > 0 && (
                  <div className="mt-2 flex flex-col gap-2">
                    <div className="flex items-center gap-2">
                      <Layers className="w-3 h-3 text-slate-600" />
                      <span className="text-[9px] font-black text-slate-600 uppercase tracking-widest">
                        More Coverage
                      </span>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {item.moreCoverage.map((link, lIdx) => (
                        <a
                          key={lIdx}
                          href={link.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          onClick={() => onTrackClick(item.title, link.source)}
                          className="text-[10px] font-bold text-orange-500/80 bg-orange-500/5 hover:bg-orange-500/10 border border-orange-500/10 px-2 py-1 rounded transition-all"
                        >
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

const VideoGrid = ({ items, onTrackClick }: { items: VideoItem[], onTrackClick: (t: string, s: string, type: string) => void }) => (
  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-10">
    {items.map((video, idx) => (
      <div key={idx} className="group relative flex flex-col">
        <div className="relative aspect-video rounded-xl overflow-hidden mb-4 ring-1 ring-white/10 group-hover:ring-orange-500/50 transition-all shadow-lg">
          {video.thumbnail ? (
            <img src={video.thumbnail} className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500" alt={video.title} />
          ) : (
            <div className="w-full h-full bg-white/5 flex items-center justify-center">
              <span className="text-white/20 text-xs uppercase tracking-widest font-black">No Preview</span>
            </div>
          )}
          <div className="absolute inset-0 bg-black/20 group-hover:bg-black/0 transition-colors" />
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
            <span className="text-xs text-white font-bold flex items-center gap-1">
              <Calendar className="w-3 h-3 text-orange-500/50" /> {formatDate(p.created_at)}
            </span>
          </div>
        </div>
        <a href={p.url} target="_blank" rel="noopener noreferrer" onClick={() => onTrackClick(p.name, p.owner, 'github_repo')} className="absolute inset-0 z-10" />
      </div>
    ))}
  </div>
);

const container = document.getElementById('root');
if (container) createRoot(container).render(<App />);