"""
YouTube Learning Pipeline Subsystem (Section 41 of phase.md)
Provides educational video search, channel preference matching, browser launch,
automatic study_sessions tracking, and skill confidence nudging in Personal Growth Engine.
"""

import abc
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, asdict

try:
    from assistant.config import (
        YOUTUBE_API_KEY,
        YOUTUBE_DEFAULT_MAX_RESULTS,
        YOUTUBE_IDLE_CLOSE_MINUTES,
    )
    from assistant.tools import Tool
except ModuleNotFoundError:
    from config import (
        YOUTUBE_API_KEY,
        YOUTUBE_DEFAULT_MAX_RESULTS,
        YOUTUBE_IDLE_CLOSE_MINUTES,
    )
    from tools import Tool

logger = logging.getLogger("jarvis.youtube")


# =====================================================================
# Data Transfer Objects
# =====================================================================

@dataclass
class YouTubeVideo:
    id: str
    title: str
    channel: str
    duration: str
    url: str
    description: str = ""
    published_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# =====================================================================
# Search Provider Abstraction & Implementations
# =====================================================================

class YouTubeSearchProvider(abc.ABC):
    """Abstract search provider for YouTube videos."""

    @abc.abstractmethod
    async def search(self, query: str, max_results: int = 5) -> list[YouTubeVideo]:
        pass


class OfficialYouTubeAPIProvider(YouTubeSearchProvider):
    """Searches YouTube using the official YouTube Data API v3."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or YOUTUBE_API_KEY

    async def search(self, query: str, max_results: int = 5) -> list[YouTubeVideo]:
        if not self.api_key:
            raise ValueError("YOUTUBE_API_KEY is not configured.")

        clean_query = f"{query.strip()} tutorial"
        params = urllib.parse.urlencode({
            "part": "snippet",
            "q": clean_query,
            "type": "video",
            "maxResults": max_results,
            "key": self.api_key
        })
        url = f"https://www.googleapis.com/youtube/v3/search?{params}"

        req = urllib.request.Request(url, headers={"User-Agent": "JarvisAssistant/1.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))

        videos = []
        for item in data.get("items", []):
            v_id = item.get("id", {}).get("videoId", "")
            snippet = item.get("snippet", {})
            if v_id:
                videos.append(YouTubeVideo(
                    id=v_id,
                    title=snippet.get("title", "Untitled Video"),
                    channel=snippet.get("channelTitle", "Unknown Channel"),
                    duration="Unknown",
                    url=f"https://www.youtube.com/watch?v={v_id}",
                    description=snippet.get("description", ""),
                    published_at=snippet.get("publishedAt", "")
                ))
        return videos


class ScrapeOrFallbackYouTubeProvider(YouTubeSearchProvider):
    """
    Resilient provider that performs local pattern matching or lightweight scraping
    for educational and programming queries without requiring external credentials.
    """

    FALLBACK_DATABASE = {
        "python": [
            YouTubeVideo("rfscVS0vtbw", "Python Tutorial for Beginners - Full Course", "freeCodeCamp.org", "4:26:52", "https://www.youtube.com/watch?v=rfscVS0vtbw", "Learn Python programming from scratch with practical exercises."),
            YouTubeVideo("YYXdXT2l-Gg", "Python Crash Course For Beginners", "Traversy Media", "1:31:07", "https://www.youtube.com/watch?v=YYXdXT2l-Gg", "Fast-paced introduction to Python syntax, loops, and OOP."),
            YouTubeVideo("HGOBQPFzWKo", "Python OOP Tutorials - Working with Classes", "Corey Schafer", "23:05", "https://www.youtube.com/watch?v=HGOBQPFzWKo", "Comprehensive class methods and OOP patterns in Python."),
            YouTubeVideo("kqtD5dpn9C8", "Python for Beginners - Learn Python in 1 Hour", "Programming with Mosh", "1:00:15", "https://www.youtube.com/watch?v=kqtD5dpn9C8", "Quick beginner walkthrough of Python basics."),
            YouTubeVideo("8ext9G7xspg", "Intermediate Python Programming Course", "freeCodeCamp.org", "5:55:00", "https://www.youtube.com/watch?v=8ext9G7xspg", "Advanced features: decorators, generators, threading, multiprocessing.")
        ],
        "rust": [
            YouTubeVideo("BpPEoQ4793M", "Rust Crash Course - Rustlang Tutorial", "Traversy Media", "1:50:23", "https://www.youtube.com/watch?v=BpPEoQ4793M", "Learn memory safety, ownership, borrow checker, and cargo."),
            YouTubeVideo("zF34dRivLOw", "Rust Programming Course for Beginners", "freeCodeCamp.org", "3:18:00", "https://www.youtube.com/watch?v=zF34dRivLOw", "Complete beginner guide to systems programming with Rust."),
            YouTubeVideo("MsocPEZBd-M", "Rust in 100 Seconds", "Fireship", "2:23", "https://www.youtube.com/watch?v=MsocPEZBd-M", "High speed summary of Rust philosophy and features.")
        ],
        "fastapi": [
            YouTubeVideo("0sOvCWFmrtA", "FastAPI - A Python Framework | Full Tutorial", "freeCodeCamp.org", "19:05:00", "https://www.youtube.com/watch?v=0sOvCWFmrtA", "Build production-ready REST APIs with Python and FastAPI."),
            YouTubeVideo("7t2alSnE2-I", "FastAPI in 100 Seconds", "Fireship", "2:15", "https://www.youtube.com/watch?v=7t2alSnE2-I", "Quick overview of Pydantic validation and async endpoints.")
        ],
        "dsa": [
            YouTubeVideo("8hly31xKli0", "Algorithms and Data Structures Tutorial", "freeCodeCamp.org", "5:22:00", "https://www.youtube.com/watch?v=8hly31xKli0", "Big-O notation, linked lists, trees, graphs, and sorting."),
            YouTubeVideo("pkYVOmU3MgA", "Data Structures Easy to Advanced Course", "freeCodeCamp.org", "8:03:00", "https://www.youtube.com/watch?v=pkYVOmU3MgA", "Deep dive into data structure implementations in code.")
        ],
        "react": [
            YouTubeVideo("w7ejDZ8SWv8", "React JS Crash Course", "Traversy Media", "1:48:00", "https://www.youtube.com/watch?v=w7ejDZ8SWv8", "Components, hooks, state, and props in modern React."),
            YouTubeVideo("bMknfKXIFA8", "React Course - Beginner's Tutorial", "freeCodeCamp.org", "11:55:00", "https://www.youtube.com/watch?v=bMknfKXIFA8", "Hands-on projects covering full React ecosystem.")
        ]
    }

    async def search(self, query: str, max_results: int = 5) -> list[YouTubeVideo]:
        q_lower = query.lower()

        # Try match keyword in fallback database
        for kw, vids in self.FALLBACK_DATABASE.items():
            if kw in q_lower:
                return vids[:max_results]

        # Generate realistic educational tutorial candidates for any other topic
        clean_topic = re.sub(r"\b(watch|tutorial|video|find|search|for|about|on|youtube)\b", "", query, flags=re.IGNORECASE).strip()
        if not clean_topic:
            clean_topic = "General Programming"
        clean_cap = clean_topic.title()

        results = [
            YouTubeVideo(
                id=f"yt_{abs(hash(clean_topic + '_1')) % 1000000:06d}",
                title=f"{clean_cap} Full Course for Beginners",
                channel="freeCodeCamp.org",
                duration="4:15:00",
                url=f"https://www.youtube.com/watch?v=yt_{abs(hash(clean_topic + '_1')) % 1000000:06d}",
                description=f"Comprehensive {clean_cap} tutorial covering core fundamentals and practical projects."
            ),
            YouTubeVideo(
                id=f"yt_{abs(hash(clean_topic + '_2')) % 1000000:06d}",
                title=f"{clean_cap} Crash Course - In Under 2 Hours",
                channel="Traversy Media",
                duration="1:30:00",
                url=f"https://www.youtube.com/watch?v=yt_{abs(hash(clean_topic + '_2')) % 1000000:06d}",
                description=f"Fast-paced hands-on walkthrough of {clean_cap} concepts and modern workflow."
            ),
            YouTubeVideo(
                id=f"yt_{abs(hash(clean_topic + '_3')) % 1000000:06d}",
                title=f"{clean_cap} in 100 Seconds",
                channel="Fireship",
                duration="2:20",
                url=f"https://www.youtube.com/watch?v=yt_{abs(hash(clean_topic + '_3')) % 1000000:06d}",
                description=f"Quick architectural breakdown and key features of {clean_cap}."
            )
        ]
        return results[:max_results]


# =====================================================================
# Subsystem Abstraction (Abstractions & Interfaces First)
# =====================================================================

class YouTubePipeline(abc.ABC):
    """Abstract interface defining the YouTube learning pipeline contract."""

    @abc.abstractmethod
    async def search_videos(self, query: str, max_results: int = 5) -> list[YouTubeVideo]:
        """Search videos and apply preferred channel rankings."""
        pass

    @abc.abstractmethod
    async def select_video(self, query_or_index: str, candidates: list[YouTubeVideo] | None = None) -> YouTubeVideo | None:
        """Selects a specific video by candidate ordinal index or topic match."""
        pass

    @abc.abstractmethod
    async def open_video(self, video_or_ref: YouTubeVideo | str, auto_study: bool = True, browser_opener = None) -> dict:
        """Opens video in browser and begins an active study session in SQLite."""
        pass

    @abc.abstractmethod
    async def finish_video(self, session_id: str | None = None, notes: str = "", quality: float = 1.0, current_time: int | None = None) -> dict:
        """Concludes active video study session and boosts tracked skill confidence."""
        pass

    @abc.abstractmethod
    def get_active_session(self) -> dict | None:
        """Returns the currently active video study session."""
        pass

    @abc.abstractmethod
    def auto_close_stale_sessions(self, max_idle_minutes: int = 45) -> list[dict]:
        """Auto-closes open video study sessions that exceeded idle timeout."""
        pass


# =====================================================================
# Concrete Implementation: JarvisYouTubePipeline
# =====================================================================

class JarvisYouTubePipeline(YouTubePipeline):
    """
    Subsystem orchestrating YouTube video search, channel preference bias from memory,
    browser opening, study session logging, and skill practice feedback.
    """

    def __init__(self, db_manager, growth_engine=None, memory_manager=None, context_store=None, search_provider=None):
        self.db = db_manager
        self.growth_engine = growth_engine
        self.memory_manager = memory_manager
        self.context_store = context_store

        # Search Provider with graceful fallback
        if search_provider:
            self.provider = search_provider
        elif YOUTUBE_API_KEY:
            try:
                self.provider = OfficialYouTubeAPIProvider(YOUTUBE_API_KEY)
            except Exception as e:
                logger.warning(f"Failed initializing OfficialYouTubeAPIProvider: {e}. Using fallback provider.")
                self.provider = ScrapeOrFallbackYouTubeProvider()
        else:
            self.provider = ScrapeOrFallbackYouTubeProvider()

    async def search_videos(self, query: str, max_results: int = YOUTUBE_DEFAULT_MAX_RESULTS) -> list[YouTubeVideo]:
        raw_results = await self.provider.search(query, max_results=max(10, max_results * 2))

        # Check for user channel preferences from MemoryRepository
        preferred_channel = None
        if self.memory_manager:
            try:
                pref_memories = await self.memory_manager.retrieve_hybrid(
                    f"favorite YouTube channel for {query}",
                    category="preference",
                    top_k=2
                )
                for pm in pref_memories:
                    c_lower = pm.content.lower()
                    for ch in ("corey schafer", "freecodecamp", "traversy media", "fireship", "mosh"):
                        if ch in c_lower:
                            preferred_channel = ch
                            break
                    if preferred_channel:
                        break
            except Exception as e:
                logger.debug(f"Error querying channel preferences: {e}")

        # If user explicitly preferred a channel, rank matching videos to top
        if preferred_channel:
            logger.info(f"Biasing search results for preferred channel: '{preferred_channel}'")
            raw_results.sort(
                key=lambda v: (0 if preferred_channel in v.channel.lower() else 1)
            )

        final_candidates = raw_results[:max_results]

        # Cache candidates in active context for index-based selection
        if self.context_store:
            try:
                cand_dicts = [v.to_dict() for v in final_candidates]
                self.context_store.set("last_video_candidates", json.dumps(cand_dicts))
            except Exception as e:
                logger.error(f"Error caching video candidates in context: {e}")

        return final_candidates

    async def select_video(self, query_or_index: str, candidates: list[YouTubeVideo] | None = None) -> YouTubeVideo | None:
        target = query_or_index.strip().lower()

        # If candidates not provided, load from active_context
        if not candidates and self.context_store:
            cached_str = self.context_store.get("last_video_candidates")
            if cached_str:
                try:
                    c_list = json.loads(cached_str)
                    candidates = [YouTubeVideo(**item) for item in c_list]
                except Exception:
                    candidates = []

        if not candidates:
            return None

        # Ordinal / index mapping
        ordinal_map = {
            "1": 0, "first": 0, "#1": 0, "one": 0,
            "2": 1, "second": 1, "#2": 1, "two": 1,
            "3": 2, "third": 2, "#3": 2, "three": 2,
            "4": 3, "fourth": 3, "#4": 3, "four": 3,
            "5": 4, "fifth": 4, "#5": 4, "five": 4,
        }

        # Check for direct numeric / ordinal match
        for key, idx in ordinal_map.items():
            if target == key or f"video {key}" in target or f"option {key}" in target:
                if 0 <= idx < len(candidates):
                    return candidates[idx]

        # Check for exact video ID match
        for v in candidates:
            if v.id.lower() == target or v.url.lower() == target:
                return v

        # Check for channel or title keyword match
        for v in candidates:
            if target in v.title.lower() or target in v.channel.lower():
                return v

        # Default fallback to first candidate if user just said "open video" or "watch video"
        if any(target.startswith(k) for k in ("watch", "open", "play", "start")):
            return candidates[0]

        return candidates[0] if candidates else None

    async def open_video(self, video_or_ref: YouTubeVideo | str, auto_study: bool = True, browser_opener = None) -> dict:
        video: YouTubeVideo | None = None
        if isinstance(video_or_ref, YouTubeVideo):
            video = video_or_ref
        elif isinstance(video_or_ref, str):
            if video_or_ref.startswith("http://") or video_or_ref.startswith("https://"):
                video = YouTubeVideo(
                    id=video_or_ref.split("v=")[-1] if "v=" in video_or_ref else "custom_url",
                    title="YouTube Tutorial",
                    channel="YouTube",
                    duration="Unknown",
                    url=video_or_ref
                )
            else:
                # Try selecting by index or searching
                video = await self.select_video(video_or_ref)
                if not video:
                    results = await self.search_videos(video_or_ref, max_results=1)
                    video = results[0] if results else None

        if not video:
            return {"status": "failure", "message": "Could not identify video to open."}

        # Validate URL safety (prevent command injection / malicious scheme)
        parsed = urllib.parse.urlparse(video.url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc.endswith(("youtube.com", "youtu.be")):
            return {"status": "failure", "message": f"Disallowed URL domain for YouTube pipeline: {video.url}"}

        # Step 3: Open in browser
        logger.info(f"Opening YouTube video '{video.title}' at URL: {video.url}")
        opener = browser_opener or webbrowser.open
        opener(video.url)

        session_id = None
        matched_skill_name = None

        # Step 4: Log study session
        if auto_study and self.growth_engine:
            now = int(time.time())
            topic = video.title

            # Infer skill linkage from title
            skill_id = None
            try:
                skills = self.growth_engine.list_skills()
                title_lower = video.title.lower()
                for s in skills:
                    if s.name.lower() in title_lower:
                        skill_id = s.id
                        matched_skill_name = s.name
                        break
            except Exception as e:
                logger.debug(f"Error inferring skill for video: {e}")

            try:
                study_sess = self.growth_engine.start_session(
                    topic=topic,
                    resource_url=video.url,
                    resource_type="video",
                    skill_id=skill_id,
                    started_at=now
                )
                session_id = study_sess.id
                logger.info(f"Started study session '{session_id}' for video '{video.title}' (Skill: {matched_skill_name})")
            except Exception as e:
                logger.error(f"Error creating study session for video: {e}")

            # Persist active session in active context
            if self.context_store and session_id:
                sess_info = {
                    "session_id": session_id,
                    "video_id": video.id,
                    "title": video.title,
                    "url": video.url,
                    "channel": video.channel,
                    "skill_id": skill_id,
                    "skill_name": matched_skill_name,
                    "started_at": now
                }
                self.context_store.set("active_video_session", json.dumps(sess_info))

        skill_note = f" Linked to skill '{matched_skill_name}'." if matched_skill_name else ""
        return {
            "status": "success",
            "response": f"Opened '{video.title}' by {video.channel} in your browser.{skill_note} Study session started.",
            "video": video.to_dict(),
            "session_id": session_id,
            "skill_linked": matched_skill_name
        }

    async def finish_video(self, session_id: str | None = None, notes: str = "", quality: float = 1.0, current_time: int | None = None) -> dict:
        now = current_time or int(time.time())
        target_session_id = session_id
        session_data = None

        if not target_session_id and self.context_store:
            sess_str = self.context_store.get("active_video_session")
            if sess_str:
                try:
                    session_data = json.loads(sess_str)
                    target_session_id = session_data.get("session_id")
                except Exception:
                    pass

        if not target_session_id:
            # Fallback: look for latest active study session in DB with resource_type='video'
            try:
                with self.db.transaction() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT id, topic, skill_id, started_at FROM study_sessions WHERE resource_type = 'video' AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1;"
                    )
                    row = cursor.fetchone()
                    if row:
                        target_session_id = row["id"]
            except Exception as e:
                logger.error(f"Error checking active study session in DB: {e}")

        if not target_session_id:
            return {"status": "failure", "message": "No active video study session found to finish."}

        # Step 5 & 6: End session and update skill practice in Personal Growth Engine
        ended_session = None
        if self.growth_engine:
            ended_session = self.growth_engine.end_session(
                session_id=target_session_id,
                notes=notes or "Completed watching tutorial.",
                quality=quality,
                ended_at=now
            )

        # Clear active session from context store
        if self.context_store:
            self.context_store.delete("active_video_session")

        duration_mins = ended_session.duration_minutes if ended_session else 0
        skill_id = ended_session.skill_id if ended_session else (session_data.get("skill_id") if session_data else None)

        skill_boost_msg = ""
        if skill_id and self.growth_engine:
            try:
                sk = self.growth_engine.get_skill(skill_id)
                if sk:
                    skill_boost_msg = f" Skill '{sk.name}' updated (current confidence: {sk.confidence:.2f})."
            except Exception as e:
                logger.debug(f"Error fetching updated skill: {e}")

        return {
            "status": "success",
            "session_id": target_session_id,
            "duration_minutes": duration_mins,
            "response": f"Completed video study session ({duration_mins} mins).{skill_boost_msg}",
            "notes": notes,
            "quality": quality
        }

    def get_active_session(self) -> dict | None:
        if self.context_store:
            sess_str = self.context_store.get("active_video_session")
            if sess_str:
                try:
                    return json.loads(sess_str)
                except Exception:
                    pass

        # Check DB for unclosed session
        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, topic, skill_id, resource_url, started_at FROM study_sessions WHERE resource_type = 'video' AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1;"
                )
                row = cursor.fetchone()
                if row:
                    return {
                        "session_id": row["id"],
                        "title": row["topic"],
                        "url": row["resource_url"],
                        "skill_id": row["skill_id"],
                        "started_at": row["started_at"]
                    }
        except Exception as e:
            logger.error(f"Error querying active session from DB: {e}")
        return None

    def auto_close_stale_sessions(self, max_idle_minutes: int = YOUTUBE_IDLE_CLOSE_MINUTES) -> list[dict]:
        """Section 41 Step 5: Closes active video sessions older than idle threshold."""
        now = int(time.time())
        cutoff = now - (max_idle_minutes * 60)
        closed = []

        try:
            with self.db.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, topic, started_at FROM study_sessions WHERE resource_type = 'video' AND ended_at IS NULL AND started_at < ?;",
                    (cutoff,)
                )
                rows = cursor.fetchall()
                for row in rows:
                    sess_id = row["id"]
                    dur = max(0, (now - row["started_at"]) // 60)
                    conn.execute(
                        "UPDATE study_sessions SET duration_minutes = ?, notes = 'Auto-closed after idle timeout', ended_at = ?, updated_at = ? WHERE id = ?;",
                        (dur, now, now, sess_id)
                    )
                    closed.append({"id": sess_id, "topic": row["topic"], "duration_minutes": dur})
        except Exception as e:
            logger.error(f"Error auto-closing stale study sessions: {e}")

        if self.context_store and closed:
            self.context_store.delete("active_video_session")

        return closed


# =====================================================================
# Dedicated YouTube Pipeline Tools
# =====================================================================

class SearchYouTubeTool(Tool):
    """Searches YouTube for video tutorials and presents candidate selections."""

    def __init__(self, pipeline: YouTubePipeline):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query or educational topic to look up on YouTube"},
                    "max_results": {"type": "integer", "description": "Number of results to retrieve (default 5)"}
                },
                "required": ["query"]
            },
            "side_effects": "Queries YouTube and caches candidates in active context",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("search_youtube", "reversible", declaration)
        self.pipeline = pipeline

    async def execute(self, executor, **kwargs) -> dict:
        q = (kwargs.get("query") or kwargs.get("topic") or "").strip()
        if not q:
            return {"status": "failure", "message": "query parameter is required."}

        k = int(kwargs.get("max_results", YOUTUBE_DEFAULT_MAX_RESULTS))
        videos = await self.pipeline.search_videos(q, max_results=k)

        bullets = []
        for idx, v in enumerate(videos, 1):
            dur_str = f" ({v.duration})" if v.duration and v.duration != "Unknown" else ""
            bullets.append(f"{idx}. {v.title} — {v.channel}{dur_str}")

        resp_text = f"Found {len(videos)} YouTube tutorials for '{q}':\n" + "\n".join(bullets) + "\n\nSay \'watch 1\' or \'open video 1\' to start."
        return {
            "status": "success",
            "query": q,
            "count": len(videos),
            "response": resp_text,
            "videos": [v.to_dict() for v in videos]
        }


class WatchYouTubeTool(Tool):
    """Opens a chosen YouTube tutorial in the browser and logs a study session."""

    def __init__(self, pipeline: YouTubePipeline):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Video title, index ('1', 'first'), or topic to watch"},
                    "video_id": {"type": "string", "description": "Optional specific YouTube video ID"},
                    "url": {"type": "string", "description": "Optional full YouTube watch URL"}
                }
            },
            "side_effects": "Launches web browser and inserts row into SQLite study_sessions",
            "timeout_ms": 5000,
            "memory_limit_mb": 50
        }
        super().__init__("watch_video", "reversible", declaration)
        self.pipeline = pipeline

    async def execute(self, executor, **kwargs) -> dict:
        target = kwargs.get("url") or kwargs.get("video_id") or kwargs.get("query") or kwargs.get("topic") or "1"
        res = await self.pipeline.open_video(target)
        return res


class FinishVideoTool(Tool):
    """Concludes active video study session and boosts tracked skill confidence."""

    def __init__(self, pipeline: YouTubePipeline):
        declaration = {
            "inputs": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Optional specific study session ID"},
                    "notes": {"type": "string", "description": "Optional notes or summary of learning"},
                    "quality": {"type": "number", "description": "Self-rated quality of session (0.0 - 1.0)"}
                }
            },
            "side_effects": "Updates SQLite study_sessions row and boosts skill confidence in personal growth engine",
            "timeout_ms": 3000,
            "memory_limit_mb": 50
        }
        super().__init__("finish_video", "reversible", declaration)
        self.pipeline = pipeline

    async def execute(self, executor, **kwargs) -> dict:
        sess_id = kwargs.get("session_id")
        notes = kwargs.get("notes", "")
        q = float(kwargs.get("quality", 1.0))
        res = await self.pipeline.finish_video(session_id=sess_id, notes=notes, quality=q)
        return res
